import os
import logging
import time
import tempfile
import requests
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from azure.ai.agents.models import CodeInterpreterTool, FilePurpose
from azure.cosmos import CosmosClient, PartitionKey
from dotenv import load_dotenv
from telemetry import create_custom_span, add_span_attributes, record_exception, create_correlation_context
try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import SpanKind as _OTEL_SpanKind
except Exception:  # opentelemetry optional at import time; function_app guards configuration
    _otel_trace = None
    _OTEL_SpanKind = None

# Optional: Agents SDK tracing helper; provide no-op fallback if unavailable
try:
    from azure.ai.agents.telemetry import trace_function as _agents_trace_function
except Exception:  # package might not be present at type-check time
    def _agents_trace_function(*_args, **_kwargs):
        def _decorator(fn):
            return fn
        return _decorator
from BlobOperations import BlobOperations_

# Load environment variables
load_dotenv()

class FoundryAgentService:
    """Service class for Azure AI Foundry Agent operations"""
    
    def __init__(self):
        self.client = AIProjectClient(
            endpoint=os.getenv("AIPROJECT_ENDPOINT"),
            credential=DefaultAzureCredential()
        )
        self.model = os.getenv("CHAT_MODEL", "gpt-4.1")
        # Use pre-created agent ID instead of creating dynamically
        self.agent_id = os.getenv("AGENT_ID")
        logging.info(f"Agent initialized: {self.agent_id}")  
        if not self.agent_id:
            logging.warning("AGENT_ID not found in environment variables. Agent will be created dynamically.")
        self._base_agent = os.getenv("AGENT_ID") 

        # try:
        #     self.client.agents.files.delete("assistant-9kfrKsrsE7kCr2KZLzbuGG")
        # except Exception as e:
        #     logging.error(f"Failed to delete file {str(e)}")

        # Unified blob storage client (single storage account, two containers)
        self.blob_ops = BlobOperations_()
        logging.info("✅ Unified blob storage client initialized (BlobOperations_)")

    # --- NEW: Helper method to determine content type from filename ---
    def _get_content_type_from_filename(self, filename: str) -> str:
        """Helper method to determine content type from filename"""
        ext = os.path.splitext(filename.lower())[1]
        content_types = {
            '.csv': 'text/csv',
            '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            '.xls': 'application/vnd.ms-excel',
            '.pdf': 'application/pdf',
            '.txt': 'text/plain',
            '.json': 'application/json',
            '.md': 'text/markdown',
            '.html': 'text/html',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
        }
        return content_types.get(ext, 'application/octet-stream')

    # --- Helper: detect if bytes represent a known image format ---
    def _detect_image_ext(self, data: bytes) -> Optional[str]:
        logging.info(f"Detecting image extension function called")
        try:
            if not data or len(data) < 4:
                return None
            # PNG
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                return ".png"
            # JPEG
            if data.startswith(b"\xff\xd8\xff"):
                return ".jpg"
            # GIF
            if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
                return ".gif"
            # SVG (text-based)
            # Try a lightweight check to avoid heavy libs
            snippet = data[:256].decode("utf-8", errors="ignore").lower()
            if "<svg" in snippet and "</svg>" not in snippet:
                # Even small snippets typically include <svg, treat as svg
                return ".svg"
            return None
        except Exception:
            return None

    # --- Helper: infer extension and content-type from bytes and optional hints ---
    def _infer_ext_and_content_type(self, data: bytes, default_ext: str = ".png", hint_text: Optional[str] = None) -> Tuple[str, str]:
        logging.info(f"Inferring extension and content type function called")
        # Image detection first
        img_ext = self._detect_image_ext(data)
        if img_ext:
            ct = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".svg": "image/svg+xml",
            }[img_ext]
            return img_ext, ct,"imgs"

        # XLSX (Zip-based with Excel structure)
        try:
            if len(data) >= 4 and data[:2] == b"PK":
                # Quick heuristic: look for 'xl/' or content types marker
                # Avoid full unzip for perf; fallback to XLSX if likely
                # This may also catch generic zips; acceptable fallback
                return ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        except Exception:
            pass

        # Text-based inference
        text = None
        try:
            text = data.decode("utf-8", errors="ignore")
        except Exception:
            text = None

        if text is not None:
            lower_all = (hint_text or "") + "\n" + text
            lower_all = lower_all.lower()
            # Prefer explicit hint in surrounding text
            if ".csv" in lower_all:
                return ".csv", "text/csv","files-gen"
            if ".json" in lower_all:
                return ".json", "application/json","files-gen"
            if ".txt" in lower_all:
                return ".txt", "text/plain",    "files-gen"

            # Heuristics: JSON vs CSV vs TXT
            stripped = text.strip()
            if (stripped.startswith("{") and ":" in stripped) or stripped.startswith("["):
                return ".json", "application/json","files-gen"
            # CSV-ish: multiple lines with commas
            if "," in text and "\n" in text:
                lines = [ln for ln in text.splitlines() if ln.strip()]
                if len(lines) >= 2:
                    comma_counts = [ln.count(",") for ln in lines[:10]]
                    if sum(1 for c in comma_counts if c > 0) >= 2:
                        return ".csv", "text/csv","files-gen"
            return ".txt", "text/plain","files-gen"

        # Fallback to default
        # Map default ext to content-type
        content_type = 'application/octet-stream'
        if default_ext in ['.png', '.jpg', '.jpeg', '.svg']:
            content_type = 'image/png' if default_ext == '.png' else ('image/svg+xml' if default_ext == '.svg' else 'image/jpeg')
            subfolder='imgs'
        elif default_ext == '.csv':
            content_type = 'text/csv'
            subfolder='files-gen'
        elif default_ext == '.xlsx':
            content_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            subfolder='files-gen'
        elif default_ext == '.json':
            content_type = 'application/json'
            subfolder='files-gen'
        elif default_ext == '.txt':
            content_type = 'text/plain'
            subfolder='files-gen'
        return default_ext, content_type,subfolder

    def upload_file(self, file_path: str) -> str:
        logging.info(f"Upload file function called for path: {file_path}")
        """Upload a file to Azure AI Foundry and return file ID"""
        with create_custom_span("foundry_upload_file", "ai_foundry"):
            try:
                add_span_attributes(
                    file_path=file_path,
                    operation="file_upload"
                )
                
                # Upload file to Azure AI Foundry for use with agents
                uploaded_file = self.client.agents.files.upload_and_poll(
                    file_path=file_path,
                    purpose=FilePurpose.AGENTS
                )
                
                add_span_attributes(
                    file_id=uploaded_file.id,
                    upload_status="success"
                )
                
                logging.info(f"File uploaded successfully: {uploaded_file.id}")
                return uploaded_file.id
            except Exception as e:
                record_exception(e, "foundry_upload_file_error")
                logging.error(f"Failed to upload file {file_path}: {str(e)}")
                raise
    
    def upload_file_from_bytes(self, file_content: bytes, filename: str) -> str:
        logging.info(f"Upload file from bytes function called for filename: {filename}")
        """Upload a file from bytes to Azure AI Foundry and return file ID.
        
        Writes bytes to a temp file, uploads to AI Foundry, cleans up, returns file_id.
        User attachments already live in the input blob container — no need to re-upload them.
        """
        with create_custom_span("foundry_upload_file_from_bytes", "ai_foundry"):
            try:
                logging.info(f"📤 Starting file upload to AI Foundry: {filename} ({len(file_content)} bytes)")

                add_span_attributes(
                    filename=filename,
                    file_size=len(file_content),
                    operation="file_upload_from_bytes"
                )
                
                # Create a temporary file and upload it
                with tempfile.NamedTemporaryFile(delete=False, suffix=f".{filename.split('.')[-1]}") as temp_file:
                    temp_file.write(file_content)
                    temp_file_path = temp_file.name
                
                try:
                    file_id = self.upload_file(temp_file_path)
                    add_span_attributes(
                        file_id=file_id,
                        upload_status="success"
                    )
                    # NEW: Enhanced logging for Azure AI Foundry upload
                    logging.info(f"🤖 File uploaded to Azure AI Foundry: {file_id}")
                    logging.info(f"✅ File upload completed successfully: {filename}")
                    return file_id
                finally:
                    # Clean up temporary file
                    if os.path.exists(temp_file_path):
                        os.unlink(temp_file_path)
            except Exception as e:
                # EXISTING CODE: Original error handling (unchanged)
                record_exception(e, "foundry_upload_file_from_bytes_error")
                logging.error(f"❌ Failed to upload file from bytes {filename}: {str(e)}")
                raise
    
    def upload_document_from_url(self, url: str) -> str:
        logging.info(f"Upload document from URL function called for URL: {url}")
        """Download document from URL and upload to Azure AI Foundry"""
        temp_file_path = None
        try:
            # Download the file
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            
            # Create temporary file
            file_extension = url.split('.')[-1].lower()
            with tempfile.NamedTemporaryFile(delete=True, suffix=f'.{file_extension}') as temp_file:
                temp_file.write(response.content)
                temp_file_path = temp_file.name
            
            # Upload to Azure AI Foundry
            file_id = self.upload_file(temp_file_path)
            return file_id
            
        except requests.exceptions.RequestException as e:
            raise Exception(f"Failed to download document from {url}: {str(e)}")
        except Exception as e:
            raise Exception(f"Failed to process document from {url}: {str(e)}")
        finally:
            # Clean up temporary file
            if temp_file_path and os.path.exists(temp_file_path):
                os.unlink(temp_file_path)
    
    def get_or_create_base_agent(self) -> str:
        """Get or create the base agent and return agent ID"""
        # Use pre-created agent if available
        if self.agent_id:
            logging.info(f"Using pre-created agent: {self.agent_id}")
            return self.agent_id
            
        # Otherwise create a new agent dynamically
        if self._base_agent is None:
            try:
                # Create a reusable base agent with code interpreter
                code_interpreter = CodeInterpreterTool()
                
                self._base_agent = self.client.agents.create_agent(
                    model=self.model,
                    name="data-analyst-agent",
                    instructions="""You are a helpful data analyst agent. You can analyze data, create visualizations, and answer questions about uploaded documents. 

Key capabilities:
- Analyze CSV, Excel, and other data files
- Create charts and visualizations using matplotlib, seaborn, or plotly
- Perform statistical analysis and data exploration
- Generate insights and summaries from data
- Answer questions about uploaded documents

Always save any charts or images you create to the current directory and provide the file paths in your response. When analyzing data, provide clear insights and actionable recommendations.""",
                    tools=code_interpreter.definitions,
                )
                logging.info(f"Dynamic agent created: {self._base_agent.id}")
            except Exception as e:
                logging.error(f"Failed to create base agent: {str(e)}")
                raise
        
        return self._base_agent.id
    
    def create_thread(self) -> str:
        """Create and return thread ID"""
        try:
            thread = self.client.agents.threads.create()
            logging.info(f"Thread created: {thread.id}")
            return thread.id
        except Exception as e:
            logging.error(f"Failed to create thread: {str(e)}")
            raise
    
    @_agents_trace_function()
    def create_message(self, thread_id: str, content: str, file_ids: List[str] = None) -> str:
        """Create a message in the thread with file attachments for code interpreter"""
        with create_custom_span("foundry_create_message", "ai_foundry"):
            try:
                add_span_attributes(
                    thread_id=thread_id,
                    content_length=len(content),
                    file_count=len(file_ids) if file_ids else 0,
                    operation="create_message"
                )
                
                # Prepare attachments for code interpreter
                attachments = []
                if file_ids:
                    logging.info(f"💬 Creating message with files | thread_id={thread_id} | file_count={len(file_ids)}")
                    for file_id in file_ids:
                        attachments.append({
                            "file_id": file_id,
                            "tools": [{"type": "code_interpreter"}]
                        })
                else:
                    logging.info(f"💬 Creating text message | thread_id={thread_id} | content_length={len(content)}")
                
                # Create message with attachments
                message = self.client.agents.messages.create(
                    thread_id=thread_id,
                    role="user",
                    content=content,
                    attachments=attachments if attachments else None
                )
                
                # SDK telemetry decorator will emit agent spans; no extra dependency span to avoid duplicates

                add_span_attributes(
                    message_id=message.id,
                    message_created=True
                )
                
                logging.info(f"✅ Message created successfully | thread_id={thread_id} | message_id={message.id} | attachments={len(attachments)}")
                return message.id
            except Exception as e:
                record_exception(e, "foundry_create_message_error")
                logging.error(f"❌ Failed to create message | thread_id={thread_id} | error={str(e)}")
                raise

    @_agents_trace_function()
    def run_thread(self, thread_id: str, agent_id: str) -> Dict[str, Any]:
        """Run the thread and wait for completion with correlation tracking"""
        with create_custom_span("foundry_run_thread", "ai_foundry"):
            try:
                add_span_attributes(
                    thread_id=thread_id,
                    agent_id=agent_id,
                    operation="run_thread"
                )
                
                logging.info(f"🤖 Starting AI agent run | thread_id={thread_id} | agent_id={agent_id}")
                
                # Use create_and_process which handles the run and waits for completion
                run = self.client.agents.runs.create_and_process(
                    thread_id=thread_id,
                    agent_id=agent_id
                )
                
                # SDK telemetry decorator will emit the start_thread_run span with usage where available

                add_span_attributes(
                    run_id=run.id,
                    run_status=run.status
                )
                




                logging.info(f"✅ AI agent run completed | thread_id={thread_id} | run_id={run.id} | status={run.status}")
                
                return {
                    "status": run.status,
                    "run_id": run.id,
                    "error": getattr(run, 'last_error', None) if run.status != 'completed' else None
                }
            except Exception as e:
                record_exception(e, "foundry_run_thread_error")
                logging.error(f"❌ AI agent run failed | thread_id={thread_id} | error={str(e)}")
                raise
    
    async def get_latest_message(self, thread_id: str, run_id: str, session_id: str = None) -> List[Dict[str, Any]]:
        """Get the latest assistant message from the thread.
        
        Downloads generated artifacts from AI Foundry and uploads them to the
        unified output blob container via self.blob_ops.
        """
        logging.info(f"Get latest message function called for thread_id: {thread_id}, run_id: {run_id}, session_id: {session_id}")
        try:
            messages = self.client.agents.messages.list(thread_id=thread_id, limit=1)
            messages_list = list(messages)
            if not messages_list:
                return []
            
            latest_message = messages_list[0]
            response_content = []

            # Process latest message content
            for content in latest_message.content:
                logging.info(f"latest msg content type: {content.type}")

                if content.type == 'text':
                    logging.info(f"output type text: {content.text.value}")
                    response_content.append({
                        "type": "text",
                        "content": content.text.value
                    })

                    # Harvest file_path annotations embedded in the text part
                    try:
                        annotations = getattr(content.text, 'annotations', []) or []
                        if annotations:
                            logging.info(f"Found {len(annotations)} text annotations; checking for file_path artifacts")
                        for ann in annotations:
                            ann_type = getattr(ann, 'type', None) or getattr(ann, 'annotation_type', None)
                            if (ann_type == 'file_path') or hasattr(ann, 'file_path'):
                                file_path_obj = getattr(ann, 'file_path', None)
                                file_id = None
                                sandbox_path = None
                                if file_path_obj is not None:
                                    file_id = getattr(file_path_obj, 'file_id', None)
                                    sandbox_path = getattr(file_path_obj, 'path', None)

                                if not file_id:
                                    file_id = getattr(ann, 'file_id', None)
                                if not sandbox_path:
                                    sandbox_path = getattr(ann, 'path', None)
                                logging.info(f"Processing text annotation for file in sandbox_path: {sandbox_path}")

                                if not sandbox_path:
                                    if hasattr(latest_message, 'file_path_annotations') and latest_message.file_path_annotations:
                                        for file_annotation in latest_message.file_path_annotations:
                                            file_id = file_annotation.file_path.file_id
                                            annotation_text = file_annotation.text if hasattr(file_annotation, 'text') else ''
                                            logging.info(f"Downloading file from annotation: {file_id}, path: {annotation_text}")
                                            sandbox_path = annotation_text

                                # Determine extension from sandbox path
                                ext = '.png'  # default
                                if isinstance(sandbox_path, str):
                                    lower = sandbox_path.lower()
                                    if lower.endswith('.png'):
                                        ext = '.png'
                                    elif lower.endswith('.jpg') or lower.endswith('.jpeg'):
                                        ext = '.jpg'
                                    elif lower.endswith('.svg'):
                                        ext = '.svg'
                                    elif lower.endswith('.csv'):
                                        ext = '.csv'
                                    elif lower.endswith('.xlsx'):
                                        ext = '.xlsx'
                                    elif lower.endswith('.json'):
                                        ext = '.json'
                                    elif lower.endswith('.txt'):
                                        ext = '.txt'

                                if file_id:
                                    try:
                                        output_filename = f"{file_id}{ext}"

                                        # Download file bytes via file_id
                                        file_stream = self.client.agents.files.get_content(file_id=file_id)
                                        content_bytes = b""
                                        for chunk in file_stream:
                                            content_bytes += chunk

                                        logging.info(f"Annotated file downloaded: {file_id} (ext: {ext}, size: {len(content_bytes)} bytes)")

                                        # If extension is ambiguous/defaulted to .png, infer a better type
                                        if ext == '.png':
                                            inferred_ext, inferred_ct, sub_folder = self._infer_ext_and_content_type(
                                                content_bytes,
                                                default_ext=ext,
                                                hint_text=(content.text.value if hasattr(content, 'text') else None)
                                            )
                                            if inferred_ext != ext:
                                                output_filename = f"{file_id}{inferred_ext}"
                                                ext = inferred_ext

                                        # Determine subfolder and content type
                                        sub_folder = self.blob_ops.get_subfolder_for_file_type(output_filename)
                                        content_type = self.blob_ops._get_content_type(output_filename)

                                        # Upload to output blob container
                                        upload_result = await self.blob_ops.upload_file(
                                            session_id=session_id or "unknown-session",
                                            file_content=content_bytes,
                                            filename=output_filename,
                                            content_type=content_type,
                                            metadata={
                                                'file_id': file_id,
                                                'source': 'text_annotation',
                                                'agent_run_id': run_id
                                            },
                                            subfolder=sub_folder
                                        )

                                        if upload_result['success']:
                                            logging.info(f"Annotated file uploaded to blob: {upload_result['blob_path']}")
                                            item_type = 'image' if ext in ['.png', '.jpg', '.jpeg', '.svg'] else 'file_output'
                                            response_content.append({
                                                "type": item_type,
                                                "file_id": file_id,
                                                "blob_url": upload_result['blob_url'],
                                                "blob_path": upload_result['blob_path'],
                                                "filename": output_filename,
                                                "session_id": session_id
                                            })
                                            # Track generated file in Cosmos DB (session doc + artifact registry)
                                            if hasattr(self, 'session_service') and self.session_service and session_id:
                                                try:
                                                    file_type = 'image' if ext in ['.png', '.jpg', '.jpeg', '.svg'] else 'file_output'
                                                    self.session_service.cosmos_service.add_generated_file(
                                                        session_id, file_id, file_type, output_filename)
                                                    self.session_service.cosmos_service.log_artifact(
                                                        session_id=session_id,
                                                        artifact_id=file_id,
                                                        artifact_type="generated_file",
                                                        metadata={"filename": output_filename, "file_type": file_type, "source": "text_annotation"}
                                                    )
                                                    logging.info(f"📝 Tracked generated file ID in Cosmos DB: {file_id}")
                                                except Exception as track_err:
                                                    logging.error(f"Failed to track generated file {file_id} in Cosmos DB: {str(track_err)}")
                                        else:
                                            logging.error(f"Failed to upload annotated file to blob: {upload_result.get('error', 'Unknown error')}")
                                            item_type = 'image' if ext in ['.png', '.jpg', '.jpeg', '.svg'] else 'file_output'
                                            response_content.append({
                                                "type": item_type,
                                                "file_id": file_id,
                                                "filename": output_filename,
                                                "blob_error": upload_result.get('error', 'Unknown error')
                                            })
                                    except Exception as ann_err:
                                        logging.error(f"Failed to fetch annotated file {file_id}: {str(ann_err)}")
                    except Exception as e:
                        logging.error(f"Failed to process text annotations: {str(e)}")

                elif content.type == 'image_file':
                    file_id = content.image_file.file_id
                    try:
                        logging.info(f"Processing image file: {file_id}")
                        image_filename = f"{file_id}.png"

                        # Get file content from Azure AI Foundry
                        file_content_stream = self.client.agents.files.get_content(file_id=file_id)
                        file_content = b""
                        for chunk in file_content_stream:
                            file_content += chunk

                        # Upload to output blob container
                        upload_result = await self.blob_ops.upload_file(
                            session_id=session_id or "unknown-session",
                            file_content=file_content,
                            filename=image_filename,
                            content_type="image/png",
                            metadata={
                                'file_id': file_id,
                                'source': 'ai_generated_image',
                                'agent_run_id': run_id
                            },
                            subfolder='imgs'
                        )

                        logging.info(f"📝 Generated image file ready for tracking: {file_id}")

                        if upload_result['success']:
                            logging.info(f"Image uploaded to blob storage: {upload_result['blob_path']}")
                            response_content.append({
                                "type": "image",
                                "file_id": file_id,
                                "blob_url": upload_result['blob_url'],
                                "blob_path": upload_result['blob_path'],
                                "filename": image_filename,
                                "session_id": session_id
                            })
                            # Track in Cosmos DB (session doc + artifact registry)
                            if hasattr(self, 'session_service') and self.session_service and session_id:
                                try:
                                    self.session_service.cosmos_service.add_generated_file(
                                        session_id, file_id, "image", image_filename)
                                    self.session_service.cosmos_service.log_artifact(
                                        session_id=session_id,
                                        artifact_id=file_id,
                                        artifact_type="generated_file",
                                        metadata={"filename": image_filename, "file_type": "image", "source": "ai_generated_image"}
                                    )
                                    logging.info(f"📝 Tracked generated file ID in Cosmos DB: {file_id}")
                                except Exception as track_err:
                                    logging.error(f"Failed to track image file {file_id} in Cosmos DB: {str(track_err)}")
                        else:
                            logging.error(f"Failed to upload image to blob storage: {upload_result.get('error', 'Unknown error')}")
                            response_content.append({
                                "type": "image",
                                "file_id": file_id,
                                "filename": image_filename,
                                "blob_error": upload_result.get('error', 'Unknown error')
                            })

                    except Exception as e:
                        logging.error(f"Failed to process image file {file_id}: {str(e)}")
                        response_content.append({
                            "type": "image",
                            "file_id": file_id,
                            "error": str(e)
                        })
            
            # Process all messages for any additional files (annotations)
        #     try:
        #         all_messages = self.client.agents.messages.list(thread_id=thread_id)
        #         logging.info(f"Inspecting all messages for additional file annotations")
        #         for msg in list(all_messages):
        #             # Check for file_path_annotations (generated files from code execution)
        #             if hasattr(msg, 'file_path_annotations') and msg.file_path_annotations:
        #                 for file_annotation in msg.file_path_annotations:
        #                     try:
        #                         file_id = file_annotation.file_path.file_id
                                
        #                         # Skip if already processed (using blob metadata for persistent tracking)
        #                         # Note: This method is a fallback, main tracking is done in get_latest_message
                                    
        #                         logging.info(f"Downloading file from annotation: {file_id}")
                                
        #                         # Determine file extension based on annotation text
        #                         annotation_text = file_annotation.text
        #                         logging.info(f"annotation_text: {annotation_text}")
        #                         file_extension = ".png" 
        #                         file_subfolder = "imgs" # Default to PNG for charts/images
        #                         if ".csv" in annotation_text.lower():
        #                             file_extension = ".csv"
        #                             file_subfolder = "files-gen"
        #                         elif ".xlsx" in annotation_text.lower():
        #                             file_extension = ".xlsx"
        #                             file_subfolder = "files-gen"
        #                         elif ".json" in annotation_text.lower():
        #                             file_extension = ".json"
        #                             file_subfolder = "files-gen"
        #                         elif ".txt" in annotation_text.lower():
        #                             file_extension = ".txt"
        #                             file_subfolder = "files-gen"
        #                         # If defaulted to .png, try inferring a better extension from content
        #                         inferred_ext, inferred_ct = (file_extension, None)
        #                         if file_extension == '.png':
        #                             inferred_ext, inferred_ct,file_subfolder = self._infer_ext_and_content_type(
        #                                 content_bytes,
        #                                 default_ext=file_extension,
        #                                 hint_text=annotation_text
        #                             )
        #                         output_filename = f"{file_id}{inferred_ext}"
        #                         # output_path = os.path.join(images_dir, output_filename) if images_dir else None
        #                         output_path=f"{session_id}/{file_subfolder}/{file_id}{inferred_ext}"
        #                         logging.info(f"Determined output filename: {output_filename}, path: {output_path}")
        #                         # Get file content bytes
        #                         stream = self.client.agents.files.get_content(file_id=file_id)
        #                         content_bytes = b""
        #                         for chunk in stream:
        #                             content_bytes += chunk

        #                         # Save locally if enabled
        #                         if output_path:
        #                             try:
        #                                 with open(output_path, 'wb') as f:
        #                                     f.write(content_bytes)
        #                             except Exception as write_err:
        #                                 logging.warning(f"Skipping local write for file annotation {output_filename}: {write_err}")

        #                         # File processed - tracking handled by blob metadata system

        #                         # Upload to blob if enabled
        #                         if self.enable_blob_storage and self.blob_service:
        #                             content_type = 'application/octet-stream'
        #                             if inferred_ext == '.csv':
        #                                 content_type = 'text/csv'
        #                             elif inferred_ext == '.xlsx':
        #                                 content_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        #                             elif inferred_ext == '.json':
        #                                 content_type = 'application/json'
        #                             elif inferred_ext == '.txt':
        #                                 content_type = 'text/plain'
        #                             elif inferred_ext in ['.png', '.jpg', '.jpeg', '.svg']:
        #                                 content_type = 'image/png' if inferred_ext == '.png' else ('image/svg+xml' if inferred_ext == '.svg' else 'image/jpeg')

        #                             subfolder=file_subfolder
        #                             upload = self.blob_service.upload_file(
        #                                 session_id=session_id or 'unknown-session',
        #                                 file_content=content_bytes,
        #                                 filename=output_filename,
        #                                 content_type=content_type,
        #                                 metadata={
        #                                     'file_id': file_id,
        #                                     'source': 'message_annotation',
        #                                     'agent_run_id': run_id
        #                                 },
        #                                 subfolder=file_subfolder
        #                             )
        #                             if upload['success']:
        #                                 item_type = 'image' if inferred_ext in ['.png', '.jpg', '.jpeg', '.svg'] else 'file_output'
        #                                 response_content.append({
        #                                     'type': item_type,
        #                                     'file_id': file_id,
        #                                     'blob_url': upload['blob_url'],
        #                                     'blob_path': upload['blob_path'],
        #                                     **({'local_path': output_path} if output_path else {}),
        #                                     'filename': output_filename,
        #                                     'annotation_text': annotation_text,
        #                                     'session_id': session_id
        #                                 })
        #                                 self.blob_service.mark_file_as_returned(upload['blob_path'])

        #                             else:
        #                                 response_content.append({
        #                                     'type': 'file_output',
        #                                     'file_id': file_id,
        #                                     **({'local_path': output_path} if output_path else {}),
        #                                     'filename': output_filename,
        #                                     'annotation_text': annotation_text,
        #                                     'blob_error': upload.get('error', 'Unknown error')
        #                                 })
        #                         else:
        #                             response_content.append({
        #                                 'type': 'file_output',
        #                                 'file_id': file_id,
        #                                 **({'local_path': output_path} if output_path else {}),
        #                                 'filename': output_filename,
        #                                 'annotation_text': annotation_text,
        #                                 **({'note': 'local storage disabled'} if not output_path else {})
        #                             })
        #                     except Exception as e:
        #                         logging.error(f"Failed to save file annotation {file_annotation.file_path.file_id}: {str(e)}")
                    
        #             # Look for code execution indicators in text content
        #             if hasattr(msg, 'content'):
        #                 for content_part in msg.content:
        #                     if content_part.type == 'text' and hasattr(content_part, 'text'):
        #                         text_value = content_part.text.value
        #                         # Look for code blocks or execution indicators
        #                         if "```python" in text_value or "matplotlib" in text_value.lower():
        #                             # Only add once per message
        #                             code_already_added = any(item.get("type") == "code_executed" for item in response_content)
        #                             if not code_already_added:
        #                                 response_content.append({
        #                                     "type": "code_executed",
        #                                     "code": "Python code execution detected in agent response"
        #                                 })
        #                             break
                        
        #     except Exception as e:
        #         logging.error(f"Failed to process message contents: {str(e)}")

        #     # Fallback: inspect run steps for artifacts and code when message lacks explicit image_file parts
            # try:
            #     self._capture_run_artifacts(
            #         thread_id=thread_id,
            #         run_id=run_id,
            #         session_id=session_id,
            #         images_dir=images_dir,
            #         response_content=response_content
            #     )
            # except Exception as e:
            #     logging.error(f"Run-step artifact capture failed: {str(e)}")

            return response_content
        except Exception as e:
            logging.error(f"Failed to get latest message: {str(e)}")
            raise

    def _capture_run_artifacts(self, thread_id: str, run_id: str, session_id: Optional[str], images_dir: str,
                                response_content: List[Dict[str, Any]]):
        logging.info(f"Capturing run artifacts from steps for thread_id: {thread_id}, run_id: {run_id}, session_id: {session_id}")
        """Best-effort: scan run steps to collect code snippets and files/images not surfaced as message parts."""
        # Try multiple SDK shapes for listing steps
        steps_iter = None
        list_attempt_errors = []
        for method_path in [
            ('agents', 'runs', 'list_steps'),        # client.agents.runs.list_steps(...)
            ('agents', 'runs', 'steps', 'list'),     # client.agents.runs.steps.list(...)
        ]:
            try:
                obj = self.client
                for attr in method_path[:-1]:
                    obj = getattr(obj, attr)
                steps_iter = getattr(obj, method_path[-1])(thread_id=thread_id, run_id=run_id)
                break
            except Exception as e:
                list_attempt_errors.append(f"{'.'.join(method_path)}: {e}")
                logging.error(f"Failed to list run steps via {'.'.join(method_path)}: {str(e)}")
                continue

        if steps_iter is None:
            logging.debug("No run steps API available: " + " | ".join(list_attempt_errors))
            return

        # Handle both list(...) and .data forms
        try:
            steps_list = list(steps_iter)
            logging.info(f"Retrieved {len(steps_list)} run steps for inspection")
            logging.info(f"Steps List Sample: {steps_list[:3]}")
        except Exception as e:
            # steps_list = getattr(steps_iter, 'data', []) or []
            logging.error(f"Failed retrieving steps, Error: {str(e)}")

        def append_code_once(code_text: str):
            if not code_text:
                return
            if any(item.get('type') == 'code_executed' and item.get('code') == code_text for item in response_content):
                return
            response_content.append({
                'type': 'code_executed',
                'code': code_text
           })

        def try_download_file(file_id: str, ext_hint: Optional[str] = None):
            if not file_id:
                return
            try:
                ext = ext_hint if ext_hint else '.png'
                filename = f"{file_id}{ext}"
                out_path = os.path.join(images_dir, filename) if images_dir else None
                stream = self.client.agents.files.get_content(file_id=file_id)
                b = b''
                for chunk in stream:
                    b += chunk
                if out_path:
                    try:
                        with open(out_path, 'wb') as f:
                            f.write(b)
                    except Exception as write_err:
                        logging.warning(f"Skipping local write for run-step file {filename}: {write_err}")
                # File processed - tracking handled by blob metadata system

                # Infer better extension if needed
                if not ext_hint or ext == '.png':
                    inferred_ext, inferred_ct ,file_subfolder= self._infer_ext_and_content_type(b, default_ext=ext)
                else:
                    inferred_ext, inferred_ct = ext, (
                        'image/png' if ext == '.png' else (
                            'image/svg+xml' if ext == '.svg' else (
                                'image/jpeg' if ext in ['.jpg', '.jpeg'] else (
                                    'text/csv' if ext == '.csv' else (
                                        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if ext == '.xlsx' else (
                                            'application/json' if ext == '.json' else (
                                                'text/plain' if ext == '.txt' else 'application/octet-stream'
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                # If we inferred a different extension, adjust filename/path
                if inferred_ext != ext:
                    ext = inferred_ext
                    filename = f"{file_id}{ext}"
                    out_path = os.path.join(images_dir, filename) if images_dir else out_path
                content_type = inferred_ct
                # Upload to blob if enabled
                if self.enable_blob_storage and self.blob_service:
                    file_subfolder="imgs" if inferred_ext in ['.png', '.jpg', '.jpeg', '.svg'] else 'files-gen'
                    upload = self.blob_service.upload_file(
                        session_id=session_id or 'unknown-session',
                        file_content=b,
                        filename=filename,
                        content_type=content_type,
                        metadata={'file_id': file_id, 'source': 'run_step', 'agent_run_id': run_id},subfolder=file_subfolder
                    )
                    if upload['success']:
                        item_type = 'image' if ext in ['.png', '.jpg', '.jpeg', '.svg'] else 'file_output'
                        response_content.append({
                            'type': item_type,
                            'file_id': file_id,
                            'blob_url': upload['blob_url'],
                            'blob_path': upload['blob_path'],
                            **({'local_path': out_path} if out_path else {}),
                            'filename': filename,
                            'session_id': session_id
                        })
                        # self.blob_service.mark_file_as_returned(upload['blob_path'])

                    else:
                        item_type = 'image' if ext in ['.png', '.jpg', '.jpeg', '.svg'] else 'file_output'
                        response_content.append({
                            'type': item_type,
                            'file_id': file_id,
                            **({'local_path': out_path} if out_path else {}),
                            'filename': filename,
                            'blob_error': upload.get('error', 'Unknown error')
                        })
                else:
                    item_type = 'image' if ext in ['.png', '.jpg', '.jpeg', '.svg'] else 'file_output'
                    response_content.append({
                        'type': item_type,
                        'file_id': file_id,
                        **({'local_path': out_path} if out_path else {}),
                        'filename': filename,
                        'session_id': session_id,
                        **({'note': 'local storage disabled'} if not out_path else {})
                    })
            except Exception as de:
                logging.error(f"Failed to download run-step file {file_id}: {str(de)}")

        # Walk each step looking for code and file_ids
        for step in steps_list:
            # Try common shapes first
            try:
                # Code input often sits on step.step_details.input or step.tool_calls[...].code_interpreter.input
                code_text = None
                for attr_path in [
                    ('step_details', 'input'),
                    ('step_details', 'code_interpreter', 'input'),
                    ('code',),
                    ('input',)
                ]:
                    obj = step
                    for a in attr_path:
                        obj = getattr(obj, a, None)
                        if obj is None:
                            break
                    if isinstance(obj, str) and obj.strip():
                        code_text = obj
                        break
                if code_text:
                    append_code_once(code_text)
            except Exception:
                pass

            # File ids often appear under outputs
            try:
                # Collect candidate (file_id, ext_hint) pairs
                candidates = []
                # Known paths where file outputs may reside
                for attr_path in [
                    ('step_details', 'output'),
                    ('step_details', 'code_interpreter', 'outputs'),
                    ('outputs',),
                    ('output',)
                ]:
                    obj = step
                    for a in attr_path:
                        obj = getattr(obj, a, None)
                        if obj is None:
                            break
                    if obj is None:
                        continue
                    # Normalize to list
                    seq = obj if isinstance(obj, (list, tuple)) else [obj]
                    for item in seq:
                        # Handle shapes: {'type': 'image', 'image': {'file_id': '...'}} or {'type': 'file', 'file_id': '...'}
                        file_id = getattr(getattr(item, 'image', None), 'file_id', None)
                        if not file_id:
                            file_id = getattr(item, 'file_id', None)
                        if not file_id and isinstance(item, dict):
                            file_id = item.get('file_id') or (item.get('image') or {}).get('file_id')

                        ext_hint = None
                        # Try to infer extension from a path if present
                        path = getattr(item, 'path', None)
                        if not path and isinstance(item, dict):
                            path = item.get('path')
                        if isinstance(path, str):
                            lp = path.lower()
                            for suffix in ['.png', '.jpg', '.jpeg', '.svg', '.csv', '.xlsx', '.json', '.txt']:
                                if lp.endswith(suffix):
                                    ext_hint = suffix
                                    break

                        if file_id:
                            candidates.append((file_id, ext_hint))

                for file_id, ext_hint in candidates:
                    try_download_file(file_id, ext_hint)
            except Exception:
                pass


class SessionService:
    """Service class for session management using local Excel file"""
    
    def __init__(self):
        self.excel_file = "threads.xlsx"
        self._ensure_excel_file_exists()
    
    def _ensure_excel_file_exists(self):
        """Create the Excel file if it doesn't exist"""
        import pandas as pd
        
        if not os.path.exists(self.excel_file):
            # Create new Excel file with required columns
            df = pd.DataFrame(columns=['thread_id', 'session_id'])
            df.to_excel(self.excel_file, index=False)
            logging.info(f"Created new Excel file: {self.excel_file}")
        else:
            logging.info(f"Using existing Excel file: {self.excel_file}")
    
    def get_thread_id(self, session_id: str) -> Optional[str]:
        """Get thread ID for a session from Excel file"""
        try:
            import pandas as pd
            
            # Read the Excel file
            df = pd.read_excel(self.excel_file)
            
            # Look for the session_id
            session_row = df[df['session_id'] == session_id]
            
            if not session_row.empty:
                thread_id = session_row.iloc[0]['thread_id']
                logging.info(f"Found existing session: {session_id} -> {thread_id}")
                return thread_id
            else:
                logging.info(f"Session not found: {session_id}")
                return None
                
        except Exception as e:
            logging.error(f"Failed to read Excel file: {str(e)}")
            return None
    
    # def save_session(self, session_id: str, thread_id: str) -> None:
    #     """Save session with thread ID to Excel file"""
    #     try:
    #         import pandas as pd
            
    #         # Read existing data
    #         df = pd.read_excel(self.excel_file)
            
    #         # Check if session already exists
    #         if session_id in df['session_id'].values:
    #             # Update existing session
    #             df.loc[df['session_id'] == session_id, 'thread_id'] = thread_id
    #             logging.info(f"Updated existing session: {session_id} -> {thread_id}")
    #         else:
    #             # Add new session
    #             new_row = pd.DataFrame({
    #                 'thread_id': [thread_id],
    #                 'session_id': [session_id]
    #             })
    #             df = pd.concat([df, new_row], ignore_index=True)
    #             logging.info(f"Added new session: {session_id} -> {thread_id}")
            
    #         # Save back to Excel
    #         df.to_excel(self.excel_file, index=False)
    #         logging.info(f"Session saved to Excel file: {self.excel_file}")
            
    #     except Exception as e:
    #         logging.error(f"Failed to save session to Excel file: {str(e)}")
    #         raise


class CosmosSessionService:
    """Service class for session management using Azure Cosmos DB with detailed logging"""
    
    def __init__(self):
        self.cosmos_endpoint = os.getenv("COSMOS_DB_ENDPOINT")
        self.database_name = os.getenv("COSMOS_DB_NAME") 
        self.container_name = os.getenv("COSMOS_CONTAINER_NAME")

        # Artifact registry container for unified file/thread retention tracking
        self.container_name_artifact_registry = os.getenv("COSMOS_CONTAINER_NAME_ARTIFACT_REGISTRY", "artifact_registry")

        # Initialize Cosmos client with API key auth and proper timeout configs
        self.cosmos_api_key = os.getenv("COSMOS_API_KEY")
        try:
            self.cosmos_client = CosmosClient(
                url=self.cosmos_endpoint, 
                credential=self.cosmos_api_key,
                timeout=300,                    # Total operation timeout in seconds (5 minutes)
                connection_timeout=60,          # HTTP connection establishment timeout
                retry_total=3,                  # Maximum retry attempts
                retry_backoff_max=30,           # Maximum retry wait time in seconds
                retry_read=2,                   # Maximum number of socket read retry attempts
                retry_connect=2,                # Maximum number of connection error retry attempts
                retry_status=3,                 # Maximum number of retry attempts on error status codes
                retry_backoff_factor=0.5,       # Factor to calculate wait time between retry attempts
                enable_endpoint_discovery=True, # Enable automatic failover
                consistency_level='Session'     # Use session consistency for better performance
            )
            
            # Get database and container references
            self.database = self.cosmos_client.get_database_client(self.database_name)
            self.container = self.database.get_container_client(self.container_name)

            # Artifact registry container (partition key: /date) — auto-create if missing
            try:
                self.database.create_container_if_not_exists(
                    id=self.container_name_artifact_registry,
                    partition_key=PartitionKey(path="/date")
                )
                logging.info(f"✅ Artifact registry container ensured: {self.container_name_artifact_registry}")
            except Exception as e:
                logging.warning(
                    f"⚠️ Could not auto-create artifact registry container "
                    f"(may already exist or insufficient permissions): {e}"
                )
            self.container_artifact_registry = self.database.get_container_client(self.container_name_artifact_registry)

            logging.info(f"Cosmos DB client initialized: {self.database_name}/{self.container_name}")
            logging.info(f"Artifact registry container: {self.container_name_artifact_registry}")
            logging.info(f"Client timeout configuration: total=300s, connection=60s, retries=3")
            
        except Exception as e:
            logging.error(f"Failed to connect to Cosmos DB: {str(e)}")
            # Re-raise to let the caller handle fallback
            raise


    # def migrate_file_ids_between_containers(
    # self, 
    # source_container_name: str, 
    # target_container_name: str)-> Dict[str, Any]:
    #     """
    #     Migrate all file_ids from source container to target container.
        
    #     Args:
    #         source_container_name: Name of source container to read from
    #         target_container_name: Name of target container to write to
            
    #     Returns:
    #         Dictionary with migration statistics
    #     """
    #     try:
    #         # Get container references
    #         source_container = self.database.get_container_client(source_container_name)
    #         target_container = self.database.get_container_client(target_container_name)
            
    #         # Query all documents from source container
    #         query = "SELECT * FROM c"
    #         items = list(source_container.query_items(
    #             query=query,
    #             enable_cross_partition_query=True
    #         ))
            
    #         logging.info(f"Found {len(items)} documents in source container: {source_container_name}")
            
    #         # Track statistics
    #         stats = {
    #             'total_documents': len(items),
    #             'sessions_processed': 0,
    #             'total_file_ids': 0,
    #             'errors': []
    #         }
            
    #         # Process each document
    #         session_file_map = {}  # {session_id: [file_ids]}
            
    #         for doc in items:
    #             try:
    #                 session_id = doc.get('session_id')
    #                 if not session_id:
    #                     continue
                    
    #                 # Initialize file list for this session
    #                 if session_id not in session_file_map:
    #                     session_file_map[session_id] = []
                    
    #                 # Extract file_ids from various fields
    #                 file_ids = []
                    
    #                 # Check uploaded_files array
    #                 if 'uploaded_files' in doc and isinstance(doc['uploaded_files'], list):
    #                     for file_entry in doc['uploaded_files']:
    #                         if isinstance(file_entry, dict) and 'file_id' in file_entry:
    #                             file_ids.append(file_entry['file_id'])
    #                         elif isinstance(file_entry, str):
    #                             file_ids.append(file_entry)
                    
    #                 # Check generated_files array
    #                 if 'generated_files' in doc and isinstance(doc['generated_files'], list):
    #                     for file_entry in doc['generated_files']:
    #                         if isinstance(file_entry, dict) and 'file_id' in file_entry:
    #                             file_ids.append(file_entry['file_id'])
    #                         elif isinstance(file_entry, str):
    #                             file_ids.append(file_entry)
                    
    #                 # Check for direct file_id field
    #                 if 'file_id' in doc:
    #                     file_ids.append(doc['file_id'])
                    
    #                 # Check for file_ids array field
    #                 if 'file_ids' in doc and isinstance(doc['file_ids'], list):
    #                     file_ids.extend(doc['file_ids'])
                    
    #                 # Add unique file_ids to session map
    #                 session_file_map[session_id].extend(file_ids)
                    
    #             except Exception as e:
    #                 error_msg = f"Error processing document {doc.get('id', 'unknown')}: {str(e)}"
    #                 logging.error(error_msg)
    #                 stats['errors'].append(error_msg)
            
    #         # Remove duplicates and write to target container
    #         for session_id, file_ids in session_file_map.items():
    #             try:
    #                 # Remove duplicates
    #                 unique_file_ids = list(set(file_ids))
                    
    #                 if unique_file_ids:
    #                     # Create document for target container
    #                     existing_files_doc = {
    #                         "id": f"{session_id}_{int(time.time())}",
    #                         "session_id": session_id,
    #                         "file_ids": unique_file_ids,
    #                         "added_at": datetime.now(timezone.utc).isoformat(),
    #                         "type": "existing_files",
    #                         "source": "migration",
    #                         "migrated_from": source_container_name
    #                     }
                        
    #                     # Write to target container
    #                     target_container.upsert_item(existing_files_doc)
                        
    #                     stats['sessions_processed'] += 1
    #                     stats['total_file_ids'] += len(unique_file_ids)
                        
    #                     logging.info(f"Migrated {len(unique_file_ids)} file IDs for session {session_id}")
                        
    #             except Exception as e:
    #                 error_msg = f"Error migrating session {session_id}: {str(e)}"
    #                 logging.error(error_msg)
    #                 stats['errors'].append(error_msg)
            
    #         logging.info(f"Migration complete: {stats}")
    #         return stats
            
    #     except Exception as e:
    #         logging.error(f"Migration failed: {str(e)}")
    #         raise

    
    def get_thread_id(self, session_id: str) -> Optional[str]:
        logging.info(f"Retrieving thread ID function called in (cosmos service): {session_id}")
        """Get thread ID for a session from Cosmos DB with robust error handling"""
        with create_custom_span("cosmos_get_thread_id", "database"):
            max_retries = 3
            retry_delay = 1  # Start with 1 second delay
            
            add_span_attributes(
                session_id=session_id,
                operation="get_thread_id",
                max_retries=max_retries
            )
            
            for attempt in range(max_retries):
                try:
                    add_span_attributes(attempt=attempt + 1)
                    
                    # Query for the session with more specific filtering
                    query = "SELECT c.thread_id FROM c WHERE c.session_id = @session_id  ORDER BY c._ts DESC"
                    parameters = [{"name": "@session_id", "value": session_id}]
                    
                    logging.info(f"Querying Cosmos DB for session: {session_id} (attempt {attempt + 1}/{max_retries})")
                    
                    # Use optimized query configuration for cross-partition queries
                    items = list(self.container.query_items(
                        query=query,
                        parameters=parameters,
                        enable_cross_partition_query=True,  # Required for cross-partition queries
                        max_item_count=1,                   # Only need the most recent one
                        populate_query_metrics=True        # Enable query metrics for debugging
                    ))
                    
                    if items:
                        thread_id = items[0]['thread_id']
                        add_span_attributes(
                            thread_id=thread_id,
                            query_result="found",
                            attempt_successful=True
                        )
                        logging.info(f"Found existing session in Cosmos DB: {session_id} -> {thread_id}")
                        return thread_id
                    else:
                        add_span_attributes(
                            query_result="not_found",
                            attempt_successful=True
                        )
                        logging.info(f"Session not found in Cosmos DB: {session_id}")
                        return None
                        
                except Exception as e:
                    record_exception(e, "cosmos_get_thread_id_error")
                    error_msg = str(e)
                    logging.error(f"Cosmos DB query attempt {attempt + 1} failed for session {session_id}: {error_msg}")
                    logging.error(f"Error type: {type(e).__name__}")
                    
                    add_span_attributes(
                        error_type=type(e).__name__,
                        error_message=error_msg,
                        attempt_failed=True
                    )
                    
                    # Check if this is a connection-related error that we should retry
                    if any(keyword in error_msg.lower() for keyword in ['connection', 'timeout', 'remote', 'network', 'aborted']):
                        if attempt < max_retries - 1:  # Don't sleep on last attempt
                            logging.info(f"Connection error detected, retrying in {retry_delay} seconds...")
                            time.sleep(retry_delay)
                            retry_delay *= 2  # Exponential backoff
                            continue
                    
                    # For non-connection errors or final attempt, return None to allow fallback
                    logging.error(f"Final attempt failed, returning None to allow graceful fallback")
                    return None
            
            add_span_attributes(all_attempts_failed=True)
            logging.error(f"All {max_retries} attempts failed for session {session_id}")
            return None
    
    def save_session(self, session_id: str, thread_id: str) -> None:
        logging.info(f"Saving session function called in (cosmos service): {session_id} -> {thread_id}")
        """Save session with thread ID to Cosmos DB with retry logic"""
        with create_custom_span("cosmos_save_session", "database"):
            max_retries = 3
            retry_delay = 1
            
            add_span_attributes(
                session_id=session_id,
                thread_id=thread_id,
                operation="save_session",
                max_retries=max_retries
            )
            
            for attempt in range(max_retries):
                try:
                    add_span_attributes(attempt=attempt + 1)
                    
                    # Create session document with file tracking
                    session_doc = {
                        "id": f"{session_id}_{int(time.time())}",
                        "session_id": session_id,
                        "thread_id": thread_id,
                        "uploaded_files": [],  # NEW: Track user uploaded file IDs
                        "generated_files": [], # NEW: Track AI generated file IDs
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "last_updated": datetime.now(timezone.utc).isoformat(),
                        "type": "session_mapping"
                    }
                    
                    # Upsert the document
                    self.container.upsert_item(session_doc)
                    
                    add_span_attributes(
                        save_result="success",
                        attempt_successful=True
                    )
                    
                    logging.info(f"Session saved to Cosmos DB: {session_id} -> {thread_id}")
                    
                    # Log thread as artifact for retention tracking
                    self.log_artifact(
                        session_id=session_id,
                        artifact_id=thread_id,
                        artifact_type="thread",
                        metadata={"source": "session_creation"}
                    )
                    
                    return  # Success, exit the retry loop
                    
                except Exception as e:
                    record_exception(e, "cosmos_save_session_error")
                    error_msg = str(e)
                    logging.error(f"Save session attempt {attempt + 1} failed: {error_msg}")
                    
                    add_span_attributes(
                        error_type=type(e).__name__,
                        error_message=error_msg,
                        attempt_failed=True
                    )
                    
                    # Check if this is a connection-related error that we should retry
                    if any(keyword in error_msg.lower() for keyword in ['connection', 'timeout', 'remote', 'network', 'aborted']):
                        if attempt < max_retries - 1:  # Don't sleep on last attempt
                            logging.info(f"Connection error detected, retrying save in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                        continue
                
                # For non-connection errors or final attempt, re-raise
                logging.error(f"Final save attempt failed, re-raising exception")
                raise
        
        logging.error(f"All {max_retries} save attempts failed for session {session_id}")
        raise Exception(f"Failed to save session after {max_retries} attempts")
    
    def add_uploaded_file(self, session_id: str, file_id: str, filename: str = None) -> None:
        logging.info(f"Adding uploaded file function called in (cosmos service): {session_id} -> {file_id}")
        """Add uploaded file ID to session tracking in Cosmos DB"""
        try:
            # Get current session document
            query = "SELECT * FROM c WHERE c.session_id = @session_id AND c.type = 'session_mapping' ORDER BY c._ts DESC"
            parameters = [{"name": "@session_id", "value": session_id}]
            
            items = list(self.container.query_items(
                query=query,
                parameters=parameters,
                enable_cross_partition_query=True,
                max_item_count=1
            ))
            
            if items:
                session_doc = items[0]
                # Add file ID to uploaded_files list if not already present
                if 'uploaded_files' not in session_doc:
                    session_doc['uploaded_files'] = []
                
                file_entry = {
                    'file_id': file_id,
                    'filename': filename,
                    'uploaded_at': datetime.now(timezone.utc).isoformat()
                }
                
                # Check if file_id already exists
                if not any(f['file_id'] == file_id for f in session_doc['uploaded_files']):
                    session_doc['uploaded_files'].append(file_entry)
                    session_doc['last_updated'] = datetime.now(timezone.utc).isoformat()
                    
                    # Update the document
                    self.container.upsert_item(session_doc)
                    logging.info(f"Added uploaded file ID to session {session_id}: {file_id}")
                else:
                    logging.info(f"File ID {file_id} already tracked for session {session_id}")
            else:
                logging.warning(f"No session document found for {session_id} to add uploaded file")
                
        except Exception as e:
            logging.error(f"Failed to add uploaded file ID to session {session_id}: {str(e)}")

    def log_artifact(self, session_id: str, artifact_id: str, artifact_type: str, metadata: Optional[Dict[str, str]] = None) -> None:
        """Log a file or thread artifact to the artifact_registry container for retention tracking.
        
        Each artifact gets one flat document. The deletion job queries by partition key (date)
        and updates deletion_status when the artifact is cleaned up.
        
        Args:
            session_id: The conversation/session ID this artifact belongs to.
            artifact_id: The Azure AI Foundry file_id or thread_id.
            artifact_type: One of 'uploaded_file', 'generated_file', or 'thread'.
            metadata: Optional dict with extra context (filename, file_type, source, etc.).
        """
        logging.info(f"Logging artifact to registry: session={session_id}, artifact={artifact_id}, type={artifact_type}")
        try:
            now = datetime.now(timezone.utc)
            date_str = now.strftime("%Y-%m-%d")
            
            artifact_doc = {
                "id": f"{artifact_id}_{int(now.timestamp() * 1000)}",
                "session_id": session_id,
                "artifact_id": artifact_id,
                "artifact_type": artifact_type,
                "date": date_str,                          # Partition key
                "created_at": now.isoformat(),
                "metadata": metadata or {},
                "deletion_status": "pending",              # pending | deleted | failed
                "deleted_at": None,
                "deletion_error": None
            }
            
            self.container_artifact_registry.upsert_item(artifact_doc)
            logging.info(f"✅ Artifact logged to registry: {artifact_type} {artifact_id} (date={date_str})")
        except Exception as e:
            logging.error(f"❌ Failed to log artifact {artifact_id} to registry: {str(e)}")
            # Don't raise — artifact logging failure shouldn't break the main flow


    
    def add_generated_file(self, session_id: str, file_id: str, file_type: str = None, filename: str = None) -> None:
        logging.info(f"Adding generated file function called in (cosmos service): {session_id} -> {file_id} ({file_type})")
        """Add generated file ID to session tracking in Cosmos DB"""
        try:
            # Get current session document
            query = "SELECT * FROM c WHERE c.session_id = @session_id AND c.type = 'session_mapping' ORDER BY c._ts DESC"
            parameters = [{"name": "@session_id", "value": session_id}]
            
            items = list(self.container.query_items(
                query=query,
                parameters=parameters,
                enable_cross_partition_query=True,
                max_item_count=1
            ))
            
            if items:
                session_doc = items[0]
                # Add file ID to generated_files list if not already present
                if 'generated_files' not in session_doc:
                    session_doc['generated_files'] = []
                
                file_entry = {
                    'file_id': file_id,
                    'filename': filename,
                    'file_type': file_type,  # 'image' or 'file_output'
                    'generated_at': datetime.now(timezone.utc).isoformat()
                }
                
                # Check if file_id already exists
                if not any(f['file_id'] == file_id for f in session_doc['generated_files']):
                    session_doc['generated_files'].append(file_entry)
                    session_doc['last_updated'] = datetime.now(timezone.utc).isoformat()
                    
                    # Update the document
                    self.container.upsert_item(session_doc)
                    logging.info(f"Added generated file ID to session {session_id}: {file_id} ({file_type})")
                else:
                    logging.info(f"Generated file ID {file_id} already tracked for session {session_id}")
            else:
                logging.warning(f"No session document found for {session_id} to add generated file")
                
        except Exception as e:
            logging.error(f"Failed to add generated file ID to session {session_id}: {str(e)}")
    
    def get_session_files(self, session_id: str) -> Dict[str, Any]:
        """Get all file IDs associated with a session"""
        logging.info(f"Getting session files function called in (cosmos service): {session_id}")
        try:
            query = "SELECT * FROM c WHERE c.session_id = @session_id AND c.type = 'session_mapping' ORDER BY c._ts DESC"
            parameters = [{"name": "@session_id", "value": session_id}]
            
            items = list(self.container.query_items(
                query=query,
                parameters=parameters,
                enable_cross_partition_query=True,
                max_item_count=1
            ))
            
            if items:
                session_doc = items[0]
                return {
                    'session_id': session_id,
                    'uploaded_files': session_doc.get('uploaded_files', []),
                    'generated_files': session_doc.get('generated_files', []),
                    'total_uploaded': len(session_doc.get('uploaded_files', [])),
                    'total_generated': len(session_doc.get('generated_files', []))
                }
            else:
                return {
                    'session_id': session_id,
                    'uploaded_files': [],
                    'generated_files': [],
                    'total_uploaded': 0,
                    'total_generated': 0
                }
                
        except Exception as e:
            logging.error(f"Failed to get session files for {session_id}: {str(e)}")
            return {
                'session_id': session_id,
                'uploaded_files': [],
                'generated_files': [],
                'total_uploaded': 0,
                'total_generated': 0,
                'error': str(e)
            }
    
    def log_interaction(self, session_id: str, thread_id: str, query: str, response: List[Dict], 
                       file_ids: List[str] = None, execution_time: float = None) -> None:
        """Log detailed interaction data to Cosmos DB"""
        try:
            interaction_doc = {
                "id": f"{session_id}_{thread_id}_{int(time.time() * 1000)}",
                "session_id": session_id,
                "thread_id": thread_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "query": query,
                "response": response,
                "file_ids": file_ids or [],
                "execution_time_seconds": execution_time,
                "response_length": len(str(response)),
                "type": "interaction_log"
            }
            
            # Save interaction log
            self.container.upsert_item(interaction_doc)
            logging.info(f"Interaction logged to Cosmos DB: {session_id}/{thread_id}")
            
        except Exception as e:
            logging.error(f"Failed to log interaction to Cosmos DB: {str(e)}")
            # Don't raise here - logging failure shouldn't break the main flow
    
    # def get_session_history(self, session_id: str, limit: int = 50) -> List[Dict]:
    #     """Get interaction history for a session"""
    #     try:
    #         query = """
    #             SELECT c.timestamp, c.query, c.response, c.file_ids, c.execution_time_seconds 
    #             FROM c 
    #             WHERE c.session_id = @session_id AND c.type = 'interaction_log'
    #             ORDER BY c.timestamp DESC
    #             OFFSET 0 LIMIT @limit
    #         """
    #         parameters = [
    #             {"name": "@session_id", "value": session_id},
    #             {"name": "@limit", "value": limit}
    #         ]
            
    #         items = list(self.container.query_items(
    #             query=query,
    #             parameters=parameters,
    #             enable_cross_partition_query=True,  # Required for cross-partition queries
    #             populate_query_metrics=True        # Enable query metrics for debugging
    #         ))
            
    #         logging.info(f"Retrieved {len(items)} interaction records for session: {session_id}")
    #         return items
            
    #     except Exception as e:
    #         logging.error(f"Failed to get session history from Cosmos DB: {str(e)}")
    #         return []


class HybridSessionService:
    """Session service that uses only Cosmos DB - no Excel fallback"""
    
    def __init__(self):
        self.cosmos_service = None
        logging.info("Initializing HybridSessionService with Cosmos DB only")
        # Only try Cosmos DB - no Excel fallback
        use_cosmos = os.getenv("USE_COSMOS_DB", "true").lower() == "true"
        if use_cosmos:
            try:
                self.cosmos_service = CosmosSessionService()
                logging.info("Successfully initialized Cosmos DB session service")
            except Exception as e:
                logging.error(f"Failed to initialize Cosmos DB: {str(e)}")
                raise  # Fail fast if Cosmos DB is not available
        else:
            raise ValueError("Cosmos DB is required but USE_COSMOS_DB is disabled")
    
    def get_thread_id(self, session_id: str) -> Optional[str]:
        """Get thread ID from Cosmos DB only"""
        if not self.cosmos_service:
            raise ValueError("Cosmos DB service not available")
        
        try:
            thread_id = self.cosmos_service.get_thread_id(session_id)
            if thread_id:
                logging.info(f"Successfully retrieved thread from Cosmos DB: {session_id} -> {thread_id}")
            else:
                logging.info(f"No thread found in Cosmos DB for session: {session_id}")
            return thread_id
        except Exception as e:
            logging.error(f"Cosmos DB lookup failed for session {session_id}: {str(e)}")
            # Return None to trigger new thread creation rather than failing
            return None
    
    def save_session(self, session_id: str, thread_id: str) -> None:
        """Save session to Cosmos DB only"""
        if not self.cosmos_service:
            raise ValueError("Cosmos DB service not available")
        
        try:
            self.cosmos_service.save_session(session_id, thread_id)
        except Exception as e:
            logging.error(f"Failed to save to Cosmos DB: {str(e)}")
            raise  # Fail if we can't save to Cosmos DB
    
    def log_interaction(self, session_id: str, thread_id: str, query: str, response: List[Dict], 
                       file_ids: List[str] = None, execution_time: float = None) -> None:
        """Log interaction to Cosmos DB"""
        if self.cosmos_service:
            try:
                self.cosmos_service.log_interaction(
                    session_id, thread_id, query, response, file_ids, execution_time
                )
            except Exception as e:
                logging.error(f"Failed to log interaction to Cosmos DB: {str(e)}")
                # Don't raise - logging failure shouldn't break the main flow
    
    # def get_session_history(self, session_id: str, limit: int = 50) -> List[Dict]:
    #     """Get session history from Cosmos DB"""
    #     if self.cosmos_service:
    #         try:
    #             return self.cosmos_service.get_session_history(session_id, limit)
    #         except Exception as e:
    #             logging.error(f"Failed to get session history from Cosmos DB: {str(e)}")
    #     return []






class ChatService:
    """Main service class that orchestrates the chat functionality"""
    
    def __init__(self):
        self.foundry_service = FoundryAgentService()
        # Use hybrid session service that handles both Cosmos DB and Excel fallback
        self.session_service = HybridSessionService()
        self.foundry_service.session_service = self.session_service


        self._agent_id = None
    
    def get_agent_id(self) -> str:
        """Get the existing agent ID"""
        return self.foundry_service.get_or_create_base_agent()
    
    
    def get_or_create_thread(self, session_id: str) -> str:
        """Get existing thread or create new one for session"""
        thread_id = self.session_service.get_thread_id(session_id)
        if not thread_id:
            logging.info(f"No existing thread for session {session_id}, creating new thread")
            thread_id = self.foundry_service.create_thread()
            self.session_service.save_session(session_id, thread_id)
            logging.info(f"created new thread {thread_id}")

        return thread_id
  
    async def process_chat_request_with_files(self, session_id: str, query: str, file_ids: List[str] = None) -> Dict[str, Any]:
        """Process a complete chat request with already uploaded file IDs"""
        with create_correlation_context(session_id=session_id) as correlation:
            start_time = time.time()
            thread_id = None
            
            try:
                correlation.log_info("🚀 Starting chat request with uploaded files",
                                   query_length=len(query),
                                   file_count=len(file_ids) if file_ids else 0)
                
                # NEW: Set current session ID on foundry service for proper blob organization
                self.foundry_service._current_session_id = session_id
                
                # Get or create thread
                correlation.log_info("🧵 Getting or creating thread")
                thread_id = self.get_or_create_thread(session_id)
                
                # Update correlation context with thread_id
                correlation.correlation_data["thread_id"] = thread_id
                correlation.log_info("🔗 Thread correlation established",
                                   thread_id=thread_id)
                
                # Create message
                correlation.log_info("💬 Creating message in thread",
                                   file_count=len(file_ids) if file_ids else 0)
                self.foundry_service.create_message(thread_id, query, file_ids or [])
                
                # Run thread
                correlation.log_info("🤖 Running AI thread")
                run_result = self.foundry_service.run_thread(thread_id, self.get_agent_id())
                
                if run_result["status"] == "completed":
                    # Get response
                    correlation.log_info("📨 Retrieving AI response")
                    response_content = await self.foundry_service.get_latest_message(thread_id, run_result['run_id'], session_id)
                    execution_time = time.time() - start_time
                    
                    correlation.log_info("✅ Chat request with files completed successfully",
                                       execution_time=execution_time,
                                       response_length=len(str(response_content)))
                    
                    # Log interaction to Cosmos DB (if available)
                    # if hasattr(self.session_service, 'log_interaction'):
                    #     correlation.log_info("💾 Logging interaction to Cosmos DB")
                    #     self.session_service.log_interaction(
                    #         session_id=session_id,
                    #         thread_id=thread_id,
                    #         query=query,
                    #         response=response_content,
                    #         file_ids=file_ids or [],
                    #         execution_time=execution_time
                    #     )
                    
                    return {
                        "session_id": session_id,
                        "thread_id": thread_id,
                        "response": response_content,
                        "status": "completed",
                        "file_ids": file_ids or []
                    }
                else:
                    correlation.log_error("❌ AI thread run failed",
                                        run_status=run_result['status'],
                                        error_details=run_result.get("error"))
                    return {
                        "session_id": session_id,
                        "thread_id": thread_id,
                        "status": "error",
                        "error": f"Run failed with status: {run_result['status']}",
                        "details": run_result.get("error")
                    }
                    
            except Exception as e:
                correlation.log_error("💥 Chat request with files processing failed",
                                    error=str(e),
                                    thread_id=thread_id)
                logging.error(f"Chat request processing failed: {str(e)}")
                return {
                    "session_id": session_id,
                    "thread_id": thread_id,
                    "status": "error",
                    "error": str(e)
                }
