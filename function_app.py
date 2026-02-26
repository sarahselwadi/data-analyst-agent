import azure.functions as func
import logging
import json
import os
import time
from contextlib import nullcontext
from typing import Optional, Dict, Any, Generator, Union

from models import CosmosSessionService  
from azurefunctions.extensions.http.fastapi import StreamingResponse, Request

from BlobOperations import BlobOperations_

# Optional Azure Monitor OpenTelemetry (guarded)
try:
    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry import trace, metrics
except Exception:  # ImportError or runtime env without these packages
    configure_azure_monitor = None
    trace = None
    metrics = None

# Configure logging
logging.basicConfig(level=logging.INFO)

# Initialize the function app
app = func.FunctionApp()

# Enable telemetry to Application Insights when configured
TELEMETRY_ENABLED = False
TRACER = None
REQUEST_COUNTER = None
LATENCY_HIST = None

try:
    appinsights_cs = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if appinsights_cs and configure_azure_monitor:
        configure_azure_monitor(connection_string=appinsights_cs)
        TELEMETRY_ENABLED = True
        if trace and metrics:
            TRACER = trace.get_tracer("clippy-data-detective")
            METER = metrics.get_meter("clippy-data-detective")
            REQUEST_COUNTER = METER.create_counter(
                name="data_analyst_requests",
                unit="1",
                description="Count of HTTP requests to data-analyst"
            )
            LATENCY_HIST = METER.create_histogram(
                name="data_analyst_latency_ms",
                unit="ms",
                description="End-to-end latency for data-analyst handler"
            )
        logging.info("✅ Application Insights telemetry enabled via OpenTelemetry")
    else:
        logging.info("ℹ️ Application Insights connection string not set or telemetry lib unavailable; telemetry disabled")
except Exception as _telemetry_err:
    TELEMETRY_ENABLED = False
    logging.warning(f"⚠️ Telemetry initialization failed: {_telemetry_err}")

def get_cors_headers():
    """Get CORS headers for frontend integration"""
    # Define allowed origins for CORS
    # allowed_origins = [
    #     "http://localhost:8080",  # Local development
    #     "https://localhost:8080", # Local development with HTTPS
    #     "http://localhost:5173",  # Vite dev server default
    #     "https://localhost:5173", # Vite dev server with HTTPS
    #     # Add your production frontend domain here when available
    # ]
    allowed_origins = "*"  # Allow all origins for now; restrict in production
    
    # Get the origin from environment or request headers
    # For Azure Functions, we'll allow localhost for development
    # In production, you should restrict this further
    origin = os.environ.get('HTTP_ORIGIN', '*')
    
    # For now, allowing localhost origins for development
    # TODO: In production, replace with your actual frontend domain
    if any(origin.startswith(allowed) for allowed in ['http://localhost:', 'https://localhost:']):
        return {
            'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Methods': 'POST, OPTIONS, GET',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization',
            'Access-Control-Max-Age': '86400'
        }
    
    # Fallback for development - remove this in production
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Methods': 'POST, OPTIONS, GET',
        'Access-Control-Allow-Headers': 'Content-Type, Authorization',
        'Access-Control-Max-Age': '86400'
    }

@app.route(route="data-analyst", auth_level=func.AuthLevel.ANONYMOUS, methods=["POST", "OPTIONS"])
async def chat_with_documents(req: Request):
    """
    Azure Function chatbot that processes documents and queries with streaming response
    Following Microsoft Docs HTTP Trigger pattern
    """
    
    # Handle CORS preflight requests
    if req.method == "OPTIONS":
        return func.HttpResponse(
            "",
            status_code=200,
            headers=get_cors_headers()
        )

    logging.info('🚀 Chat function triggered')

    start_time = time.time()
    resp = None
    status_code = 500

    ctx = TRACER.start_as_current_span("data_analyst") if TRACER else nullcontext()
    with ctx as span:
        try:
            # if span and hasattr(span, "set_attribute"):
            #     span.set_attribute("fa.route", "data-analyst")
            #     span.set_attribute("http.method", req.method)
            #     span.set_attribute("http.scheme", "https" if str(req.url).startswith("https") else "http")

            # Import models only when needed to avoid module loading issues
            from models import ChatService
            chat_service = ChatService()
            logging.info("🤖 ChatService initialized")

            # Handle both form data (file uploads) and JSON requests
            logging.info(f"📝 Request method: {req.method}")
            logging.info(f"🌐 Request Header: {req.url}")
            logging.info(f"📥 Request content-type: {req.headers.get('content-type', '')}")
            logging.info("Start Request Processing ....")
            
            resp = await _handle_multipart_request(req, chat_service)

        except ImportError as e:
            logging.error(f"❌ Import error: {str(e)}")
            if span and hasattr(span, "record_exception"):
                span.record_exception(e)
                span.set_attribute("error.type", "ImportError")
            # Always return error; no mock responses
            resp = func.HttpResponse(
                json.dumps({
                    "error": "Service configuration error",
                    "message": "Dependencies are not available. Please check server logs."
                }),
                status_code=500,
                mimetype="application/json",
                headers=get_cors_headers()
            )

        except Exception as e:
            logging.error(f"❌ Unexpected error in chat function: {str(e)}", exc_info=True)
            if span and hasattr(span, "record_exception"):
                span.record_exception(e)
                span.set_attribute("error", True)
            resp = func.HttpResponse(
                json.dumps({
                    "error": "Internal server error",
                    "message": "An unexpected error occurred while processing your request"
                }),
                status_code=500,
                mimetype="application/json",
                headers=get_cors_headers()
            )
        finally:
            # Metrics and span attributes
            duration_ms = int((time.time() - start_time) * 1000)
            if resp is not None:
                try:
                    status_code = getattr(resp, "status_code", status_code) or status_code
                except Exception:
                    pass
            if REQUEST_COUNTER:
                REQUEST_COUNTER.add(1, attributes={
                    "route": "data-analyst",
                    "method": req.method,
                    "status_code": str(status_code),
                })
            if LATENCY_HIST:
                LATENCY_HIST.record(duration_ms, attributes={
                    "route": "data-analyst",
                    "method": req.method,
                    "status_code": str(status_code),
                })
            if span and hasattr(span, "set_attribute"):
                span.set_attribute("app.duration_ms", duration_ms)
                span.set_attribute("http.status_code", status_code)

    return resp  # type: ignore[return-value]

async def _handle_multipart_request(req: Request, chat_service):
    """Handle multipart/form-data requests (file uploads)"""
    try:
        # Handle file uploads

        body = await req.json()

        session_id = body.get('conversationId')
        query = body.get('query')
        attachments =body.get("attachments") or []

        file_ids = []
        if len(attachments) > 0:
            BlobOperations = BlobOperations_()
            for attachment in attachments:
                logging.info(f"Attachment found: {attachment.get('fileName')} ({attachment.get('size', 'unknown')} bytes)")

                attachment_url = attachment['url']
                attachment_name = attachment['fileName']

                attachment_path, attachment_bytes = await BlobOperations.read_blob(attachment_url)

                file_id = chat_service.foundry_service.upload_file_from_bytes(
                        attachment_bytes, 
                        attachment_name
                    )
                file_ids.append(file_id)
            
        
        logging.info(f"📂 Multipart request - Session: {session_id}, Query length: {len(query) if query else 0}, Files: {len(attachments) if attachments else 0}")
        
        if not session_id or not query:
            logging.error("❌ Missing required fields (session_id or query)")
            return func.HttpResponse(
                json.dumps({"error": "session_id and query are required"}),
                status_code=400,
                mimetype="application/json",
                headers=get_cors_headers()
            )
        
        # Process uploaded files
        # file_ids = []
        # for file in files:
        #     try:
        #         file_content = file.read()
        #         logging.info(f"📤 Uploading file: {file.filename} ({len(file_content)} bytes)")
                
        #         # Set session_id on foundry service before uploading
        #         chat_service.foundry_service._current_session_id = session_id
                
        #         file_id = chat_service.foundry_service.upload_file_from_bytes(
        #             file_content, 
        #             file.filename
        #         )
        #         file_ids.append(file_id)
        #         logging.info(f"✅ File uploaded successfully: {file.filename} -> {file_id}")
                

        #     except Exception as e:
        #         logging.error(f"❌ File upload failed for {file.filename}: {str(e)}")
        #         return func.HttpResponse(
        #             json.dumps({"error": f"Failed to upload file {file.filename}: {str(e)}"}),
        #             status_code=400,
        #             mimetype="application/json",
        #             headers=get_cors_headers()
        #         )
        
        # Process the chat request
        logging.info("🤖 Starting AI chat completion with files")
        result = chat_service.process_chat_request_with_files(
            session_id=session_id,
            query=query,
            file_ids=file_ids
        )
        
        # NEW: Track uploaded file ID in Cosmos DB
        if hasattr(chat_service.session_service, 'cosmos_service') and chat_service.session_service.cosmos_service:
            for file_id in file_ids:
                try:
                    chat_service.session_service.cosmos_service.add_uploaded_file(
                        session_id=session_id,
                        file_id=file_id,
                    )
                    logging.info(f"📝 Tracked uploaded file ID in Cosmos DB: {file_id}")
                    chat_service.session_service.cosmos_service.add_existing_files(
                        session_id=session_id,
                        file_ids=file_ids,
                    )
                    logging.info(f"📝 Tracked existing file IDs in Cosmos DB for session: {session_id}")
                except Exception as e:
                    logging.warning(f"⚠️ Failed to track uploaded file ID in Cosmos DB: {str(e)}")
        
        logging.info(f"✅ AI chat completion finished: {result.get('status', 'unknown')}")
        return _create_response(result)
        
    except Exception as e:
        logging.error(f"❌ Error in multipart request: {str(e)}", exc_info=True)
        raise

# Mock handler removed: no mock responses in this service

def _stream_response(result: dict):
    """Generator function to stream response with SSE format"""
    try:
        # Import here to access the helper method
        from models import FoundryAgentService
        foundry_helper = FoundryAgentService()
        
        # 1. Yield text content first
        text_content = []
        for item in result.get("response", []):
            if item.get("type") == "text":
                text_content.append(item.get("content", ""))
        
        if text_content:
            assistant_reply = "\n".join(text_content)
            yield f"{assistant_reply}\n\n"
            logging.info(f"📤 Streamed text content: {len(assistant_reply)} chars")
        
        # 2. Build and yield attachments event
        attachments = []
        for item in result.get("response", []):
            if item.get("type") in ["image", "file_output"]:
                filename = item.get("filename", "")
                file_type = filename.split('.')[-1].lower() if '.' in filename else "png"
                
                attachment = {
                    "type": file_type,
                    "fileId": item.get("file_id"),
                    "fileName": filename,
                    "url": item.get("blob_url") or item.get("sas_url"),
                    "metadata": {
                        "size": 0,  # Size set to 0 as per requirement
                        "contentType": foundry_helper._get_content_type_from_filename(filename)
                    }
                }
                attachments.append(attachment)
        
        if attachments:
            attachments_json = json.dumps(attachments)
            yield f"event: attachments\n{attachments_json}\n\n"
            logging.info(f"📎 Streamed {len(attachments)} attachments")
        
    except Exception as e:
        logging.error(f"❌ Error in stream generator: {str(e)}", exc_info=True)
        # Stream error event
        error_data = json.dumps({
            "error": "Stream generation error",
            "message": str(e)
        })
        yield f"event: error\n{error_data}\n\n"

def _stream_error(result: dict):
    """Generator function to stream error events"""
    try:
        error_data = {
            "error": result.get("error", "Unknown error"),
            "message": result.get("details", "An error occurred while processing your request"),
            "status": result.get("status", "error")
        }
        error_json = json.dumps(error_data)
        yield f"event: error\n{error_json}\n\n"
        logging.error(f"📤 Streamed error event: {error_data}")
    except Exception as e:
        logging.error(f"❌ Error in error stream generator: {str(e)}", exc_info=True)
        yield f"event: error\n{json.dumps({'error': 'Critical error', 'message': str(e)})}\n\n"

def _create_response(result: dict):
    """Create streaming HTTP response"""
    logging.info(f"📦 Creating streaming response for result: {result.get('status', 'unknown')}")

    if result.get("status") == "completed":
        logging.info("✅ Creating successful streaming response")
        return StreamingResponse(
            _stream_response(result),
            media_type="text/event-stream",
            headers={
                **get_cors_headers(),
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            }
        )
    else:
        logging.error(f"❌ Creating error streaming response: {result.get('status', 'unknown')}")
        return StreamingResponse(
            _stream_error(result),
            media_type="text/event-stream",
            headers={
                **get_cors_headers(),
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no'
            }
        )
