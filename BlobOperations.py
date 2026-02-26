"""
Unified Azure Blob Storage client for the Data Analyst Agent.

Handles:
- Reading client attachments from the INPUT container (O_CONTAINER_NAME)
- Writing generated artifacts to the OUTPUT container (OUTPUT_CONTAINER_NAME)
- Generating account-key-based SAS URLs for output files
- Auto-creating the output container on first use
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional, Dict, Any, List

from azure.storage.blob.aio import BlobServiceClient, ContainerClient
from azure.storage.blob import generate_blob_sas, BlobSasPermissions
from azure.core.exceptions import ResourceExistsError


class BlobOperations_:
    """
    Single unified blob client backed by one storage account
    (connection-string / account-key auth).

    Two containers:
      • input  – O_CONTAINER_NAME          (read-only, client attachments)
      • output – OUTPUT_CONTAINER_NAME      (read/write, generated artifacts)
    """

    # Subfolder constants for organized output storage
    SUBFOLDER_IMAGES = "imgs"
    SUBFOLDER_FILES_GEN = "files-gen"

    def __init__(self, max_workers: int = 10):
        """
        :param max_workers: Reserved for future parallel operations.
        """
        self.max_workers = max_workers

        # ── connection string & service client ──────────────────────────
        conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
        if not conn_str:
            raise ValueError("AZURE_STORAGE_CONNECTION_STRING environment variable is required")

        self._client: BlobServiceClient = BlobServiceClient.from_connection_string(conn_str)

        # Parse account name and account key from the connection string
        # (needed for account-key-based SAS generation)
        self._account_name: Optional[str] = None
        self._account_key: Optional[str] = None
        for part in conn_str.split(";"):
            part = part.strip()
            if part.lower().startswith("accountname="):
                self._account_name = part.split("=", 1)[1]
            elif part.lower().startswith("accountkey="):
                self._account_key = part.split("=", 1)[1]

        # ── input container (attachments – read only) ───────────────────
        self.container_name: str = os.getenv("O_CONTAINER_NAME", "")
        self.container: ContainerClient = self._client.get_container_client(self.container_name)

        # ── output container (generated artifacts – read/write) ─────────
        self.output_container_name: str = os.getenv("OUTPUT_CONTAINER_NAME", "")
        self._output_container: Optional[ContainerClient] = None
        self._output_container_exists: bool = False

    # ──────────────────────────────────────────────────────────────────────
    # Input container helpers
    # ──────────────────────────────────────────────────────────────────────

    def extract_blob_path(self, url: str, container_name: str) -> str:
        """
        Extract the blob path from a full blob URL.

        Handles any file extension (not just .csv).
        Strips query-string parameters (e.g. SAS tokens) before extracting.
        """
        # Remove query string if present
        clean_url = url.split("?")[0]

        idx = clean_url.find(container_name)
        if idx == -1:
            raise ValueError(f"Container name '{container_name}' not found in URL")

        # Everything after  <container_name>/
        start = idx + len(container_name) + 1
        return clean_url[start:]

    async def read_blob(self, blob_path: str) -> Tuple[str, bytes]:
        """
        Read a single blob from the **input** container and return (path, bytes).
        """
        if blob_path.startswith("http"):
            blob_path = self.extract_blob_path(blob_path, self.container_name)

        blob_client = self.container.get_blob_client(blob=str(blob_path))
        downloader = await blob_client.download_blob()
        data = await downloader.readall()
        logging.info(f"Read blob '{blob_path}' from input container '{self.container_name}'.")
        return blob_path, data

    # ──────────────────────────────────────────────────────────────────────
    # Output container helpers
    # ──────────────────────────────────────────────────────────────────────

    async def _ensure_output_container_exists(self) -> ContainerClient:
        """
        Lazily create the output container (idempotent).
        Returns the ContainerClient for the output container.
        """
        if self._output_container is not None and self._output_container_exists:
            return self._output_container

        self._output_container = self._client.get_container_client(self.output_container_name)

        if not self._output_container_exists:
            try:
                await self._output_container.create_container()
                logging.info(f"📦 Created output container '{self.output_container_name}'")
            except ResourceExistsError:
                logging.info(f"📦 Output container '{self.output_container_name}' already exists")
            except Exception as e:
                # Some SDK versions raise a generic error with "ContainerAlreadyExists"
                if "ContainerAlreadyExists" in str(e):
                    logging.info(f"📦 Output container '{self.output_container_name}' already exists")
                else:
                    logging.error(f"❌ Failed to create output container: {e}")
                    raise
            self._output_container_exists = True

        return self._output_container

    @staticmethod
    def _generate_blob_path(session_id: str, filename: str, subfolder: Optional[str] = None) -> str:
        """
        Build blob path: {session_id}/{subfolder}/{filename}
        Falls back to {session_id}/{filename} when subfolder is None.
        """
        session_id = session_id.replace("/", "_").replace("\\", "_")
        filename = os.path.basename(filename)

        if subfolder:
            subfolder = subfolder.replace("/", "_").replace("\\", "_")
            return f"{session_id}/{subfolder}/{filename}"
        return f"{session_id}/{filename}"

    async def upload_file(
        self,
        session_id: str,
        file_content: bytes,
        filename: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
        subfolder: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Upload a file to the **output** container.

        Returns a dict with keys:
            success, blob_url (=sas_url), sas_url, blob_path, filename,
            session_id, content_type, size, metadata
        """
        try:
            container = await self._ensure_output_container_exists()

            blob_path = self._generate_blob_path(session_id, filename, subfolder=subfolder)

            # Metadata
            blob_metadata: Dict[str, str] = {
                "session_id": session_id,
                "original_filename": filename,
                "upload_timestamp": datetime.now(timezone.utc).isoformat(),
                "file_size": str(len(file_content)),
            }
            if metadata:
                blob_metadata.update(metadata)

            if not content_type:
                content_type = self._get_content_type(filename)

            blob_client = container.get_blob_client(blob_path)
            await blob_client.upload_blob(
                data=file_content,
                content_type=content_type,
                metadata=blob_metadata,
                overwrite=True,
            )

            sas_url = self.generate_sas_url(blob_path, expiry_hours=24)

            logging.info(f"📤 Uploaded file: {filename} -> {blob_path}")

            return {
                "success": True,
                "blob_url": sas_url,
                "sas_url": sas_url,
                "blob_path": blob_path,
                "filename": filename,
                "session_id": session_id,
                "content_type": content_type,
                "size": len(file_content),
                "metadata": blob_metadata,
            }

        except Exception as e:
            logging.error(f"❌ Error uploading {filename}: {e}")
            return {
                "success": False,
                "error": f"Upload error: {e}",
                "filename": filename,
            }

    # ──────────────────────────────────────────────────────────────────────
    # SAS URL generation (account-key based, sync utility)
    # ──────────────────────────────────────────────────────────────────────

    def generate_sas_url(self, blob_path: str, expiry_hours: int = 24) -> str:
        """
        Generate a read-only SAS URL for a blob in the **output** container
        using the storage account key (no user-delegation key needed).
        """
        if not self._account_name or not self._account_key:
            raise ValueError(
                "Account name and key must be available in the connection string "
                "to generate account-key-based SAS tokens."
            )

        sas_token = generate_blob_sas(
            account_name=self._account_name,
            account_key=self._account_key,
            container_name=self.output_container_name,
            blob_name=blob_path,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(hours=expiry_hours),
        )

        blob_url = (
            f"https://{self._account_name}.blob.core.windows.net/"
            f"{self.output_container_name}/{blob_path}"
        )
        return f"{blob_url}?{sas_token}"

    # ──────────────────────────────────────────────────────────────────────
    # Subfolder routing
    # ──────────────────────────────────────────────────────────────────────

    def get_subfolder_for_file_type(self, filename: str, file_source: Optional[str] = None) -> str:
        """
        Determine the appropriate output subfolder based on file type / source.
        """
        if file_source == "imgs":
            return self.SUBFOLDER_IMAGES
        if file_source == "files-gen":
            return self.SUBFOLDER_FILES_GEN

        ext = os.path.splitext(filename.lower())[1]

        if ext in (".png", ".jpg", ".jpeg", ".gif", ".svg"):
            return self.SUBFOLDER_IMAGES

        # Everything else (csv, xlsx, json, txt, pdf, docx, …) → files-gen
        return self.SUBFOLDER_FILES_GEN

    # ──────────────────────────────────────────────────────────────────────
    # Content-type helper
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_content_type(filename: str) -> str:
        """Determine MIME type from file extension."""
        ext = os.path.splitext(filename.lower())[1]
        content_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
            ".pdf": "application/pdf",
            ".json": "application/json",
            ".txt": "text/plain",
            ".csv": "text/csv",
            ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            ".xls": "application/vnd.ms-excel",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".md": "text/markdown",
            ".html": "text/html",
            ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
        return content_types.get(ext, "application/octet-stream")

    # ──────────────────────────────────────────────────────────────────────
    # Listing helpers
    # ──────────────────────────────────────────────────────────────────────

    async def list_session_files(self, session_id: str, date: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        List all files in the **output** container for a given session.
        """
        try:
            container = await self._ensure_output_container_exists()
            prefix = f"{session_id}/"
            if date:
                prefix = f"{session_id}/{date}/"

            files: List[Dict[str, Any]] = []
            async for blob in container.list_blobs(name_starts_with=prefix):
                files.append({
                    "name": blob.name,
                    "size": blob.size,
                    "last_modified": blob.last_modified,
                    "metadata": blob.metadata,
                })
            return files

        except Exception as e:
            logging.error(f"❌ Error listing files for session {session_id}: {e}")
            return []

    # ──────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────────

    async def close(self):
        """Close the underlying blob service client (and any container clients)."""
        await self._client.close()
