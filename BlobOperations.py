from azure.storage.blob.aio import BlobServiceClient
from azure.storage.blob import generate_blob_sas, BlobSasPermissions
from typing import Tuple
import os

class BlobOperations_:
    def __init__(
        self,
        max_workers: int = 10,
    ):
        """
        :param container_name: Target container name.
        :param max_workers: (int, optional): Number of threads to use for moving blobs. Default = 10.
        :param create_container: (bool, optional): Whether to create container if it doesn't exist. Default = False.
        """
        self.container_name =  os.getenv("O_CONTAINER_NAME")
        self.max_workers = max_workers
        conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

        self._client = BlobServiceClient.from_connection_string(conn_str)
        self.container = self._client.get_container_client(self.container_name)

    def extract_blob_path(self, url: str, container_name: str) -> str:
        # Find where the container name appears in the URL path
        idx = url.find(container_name)
        if idx == -1:
            raise ValueError(f"Container name '{container_name}' not found in URL")
        # Extract everything *after* container name + one slash
        start = idx + len(container_name) + 1  
        end = url.lower().index('.csv') + 4  # 4 = len('.csv')
        return url[start:end]
    
    async def read_blob(
        self,
        blob_path: str
    ) -> Tuple[str, bytes]:
        """
        Read a single blob and return its path and content bytes.

        Parameters:
        - blob_path (str): Path of the blob inside the container.

        Returns:
        - Tuple[str, bytes]: (blob_path, blob data bytes)
        """

        # Get blob client and download
        if blob_path.startswith("http"):
            blob_path = self.extract_blob_path(blob_path, self.container_name)
        else:
            blob_path = blob_path      
        print(blob_path)
        blob_client = self.container.get_blob_client(blob=str(blob_path))
        downloader =  await blob_client.download_blob()
        data = await downloader.readall()
        print(f"Read blob '{blob_path}' from container '{self.container_name}'.")
        return blob_path, data 

    async def close(self):
        """Close the blob service client."""
        await self._client.close()
