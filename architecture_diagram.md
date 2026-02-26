# Clippy Data Detective — Architecture & Flow Diagrams

## 1. High-Level System Architecture

```mermaid
graph TB
    subgraph "Client Layer"
        CLIENT["🌐 Web Client<br/>(test.html / Frontend SPA)"]
    end

    subgraph "Azure Function App"
        FA["⚡ function_app.py<br/>HTTP Trigger: POST /api/data-analyst"]
        CORS["🔒 CORS Handler<br/>get_cors_headers()"]
        STREAM["📡 Streaming Response<br/>_stream_response() / _stream_error()"]
        MULTIPART["📦 Request Handler<br/>_handle_multipart_request()"]
    end

    subgraph "Service Layer (models.py)"
        CS["🤖 ChatService<br/>Orchestrator"]
        FAS["🧠 FoundryAgentService<br/>AI Agent Operations"]
        HSS["🔗 HybridSessionService<br/>Session Management"]
        CSS["💾 CosmosSessionService<br/>Cosmos DB Operations"]
    end

    subgraph "External Blob Reader"
        BOPS["📥 BlobOperations_<br/>(BlobOperations.py)<br/>Read attachments from<br/>external blob storage"]
    end

    subgraph "Storage Layer"
        BLOB["☁️ BlobStorageService<br/>(blob_service.py)<br/>Session artifact storage"]
    end

    subgraph "Telemetry Layer"
        TEL["📊 telemetry.py<br/>OpenTelemetry + App Insights"]
    end

    subgraph "Azure Services"
        AI["🤖 Azure AI Foundry<br/>Agents API<br/>(Threads/Messages/Runs)"]
        COSMOS["💾 Azure Cosmos DB<br/>Session Mapping<br/>+ File Tracking"]
        BLOBAZ["☁️ Azure Blob Storage<br/>Session Files Container"]
        APPINS["📈 Application Insights<br/>Monitoring & Tracing"]
        EXTBLOB["📦 External Blob Storage<br/>(User attachment source)"]
    end

    CLIENT -->|"POST JSON<br/>{conversationId, query, attachments}"| FA
    FA --> CORS
    FA --> MULTIPART
    MULTIPART --> CS
    MULTIPART -->|"Read attachment bytes"| BOPS
    BOPS -->|"Download blob"| EXTBLOB
    CS --> FAS
    CS --> HSS
    HSS --> CSS
    CSS -->|"CRUD sessions & files"| COSMOS
    FAS -->|"Upload files,<br/>Create threads/messages,<br/>Run agent"| AI
    FAS -->|"Store generated artifacts"| BLOB
    BLOB -->|"Upload/Download"| BLOBAZ
    FA --> STREAM
    STREAM -->|"SSE text/event-stream"| CLIENT
    FA -.->|"Spans & Metrics"| TEL
    TEL -.->|"Export"| APPINS
    CS -.->|"Correlation Context"| TEL
    FAS -.->|"Custom Spans"| TEL
    CSS -.->|"Custom Spans"| TEL
```

---

## 2. Complete Request Processing Sequence

```mermaid
sequenceDiagram
    autonumber
    participant C as 🌐 Client
    participant FA as ⚡ Function App
    participant BO as 📥 BlobOperations_
    participant CS as 🤖 ChatService
    participant FAS as 🧠 FoundryAgentService
    participant HSS as 🔗 HybridSessionService
    participant CSS as 💾 CosmosSessionService
    participant AI as 🤖 Azure AI Foundry
    participant BS as ☁️ BlobStorageService
    participant DB as 💾 Cosmos DB

    Note over C,FA: 1. HTTP Request
    C->>FA: POST /api/data-analyst<br/>{conversationId, query, attachments[]}

    Note over FA: CORS check + Telemetry span start
    FA->>FA: get_cors_headers()
    FA->>FA: TRACER.start_as_current_span("data_analyst")
    FA->>FA: ChatService() initialized

    Note over FA,BO: 2. Process Attachments (if any)
    FA->>FA: Parse JSON body → session_id, query, attachments

    loop For each attachment
        FA->>BO: BlobOperations_().read_blob(attachment_url)
        BO-->>FA: (path, bytes)
        FA->>FAS: upload_file_from_bytes(bytes, fileName)
        Note over FAS: Write to temp file
        FAS->>AI: agents.files.upload_and_poll(temp_file)
        AI-->>FAS: file_id
        FAS-->>FA: file_id
        FA->>FA: file_ids.append(file_id)
    end

    Note over FA,CS: 3. Process Chat Request
    FA->>CS: process_chat_request_with_files(session_id, query, file_ids)

    Note over CS,CSS: 4. Session Management
    CS->>HSS: get_thread_id(session_id)
    HSS->>CSS: get_thread_id(session_id)
    CSS->>DB: SELECT thread_id WHERE session_id=@sid
    DB-->>CSS: thread_id or null

    alt New Session (no thread_id)
        CS->>FAS: create_thread()
        FAS->>AI: agents.threads.create()
        AI-->>FAS: thread object
        FAS-->>CS: thread_id
        CS->>HSS: save_session(session_id, thread_id)
        HSS->>CSS: save_session(session_id, thread_id)
        CSS->>DB: Upsert session_mapping doc
    end

    Note over CS,AI: 5. AI Agent Interaction
    CS->>FAS: create_message(thread_id, query, file_ids)
    FAS->>AI: agents.messages.create(thread_id, content, attachments[code_interpreter])
    AI-->>FAS: message_id

    CS->>FAS: run_thread(thread_id, agent_id)
    FAS->>AI: agents.runs.create_and_process(thread_id, agent_id)
    Note over AI: Agent executes code interpreter<br/>Generates text, images, files
    AI-->>FAS: run {status, run_id}

    Note over CS,BS: 6. Response Extraction
    CS->>FAS: get_latest_message(thread_id, run_id, session_id)
    FAS->>AI: agents.messages.list(thread_id, limit=1)
    AI-->>FAS: message with content[]

    loop For each content item
        alt type == "text"
            FAS->>FAS: Extract text value
            FAS->>FAS: Process file_path annotations
            loop For each file_path annotation
                FAS->>AI: agents.files.get_content(file_id)
                AI-->>FAS: file bytes
                FAS->>FAS: _infer_ext_and_content_type(bytes)
                FAS->>BS: upload_file(session_id, bytes, filename, subfolder)
                BS-->>FAS: {blob_url, sas_url, blob_path}
                FAS->>CSS: add_generated_file(session_id, file_id)
                FAS->>CSS: add_existing_files(session_id, file_ids)
            end
        else type == "image_file"
            FAS->>AI: agents.files.get_content(file_id)
            AI-->>FAS: image bytes
            FAS->>BS: upload_file(session_id, bytes, filename, subfolder="imgs")
            BS-->>FAS: {blob_url, sas_url, blob_path}
            FAS->>CSS: add_generated_file(session_id, file_id, "image")
            FAS->>CSS: add_existing_files(session_id, file_id)
        end
    end

    FAS-->>CS: response_content[]

    Note over CS,FA: 7. Track Uploaded Files in Cosmos
    loop For each uploaded file_id
        FA->>CSS: add_uploaded_file(session_id, file_id)
        FA->>CSS: add_existing_files(session_id, file_ids)
    end

    Note over FA,C: 8. Streaming Response
    CS-->>FA: result {status, response[], session_id, thread_id}
    FA->>FA: _create_response(result)
    FA->>FA: StreamingResponse(_stream_response(result))

    Note over FA: SSE Format:<br/>Text content first<br/>Then "event: attachments" with JSON
    FA-->>C: text/event-stream<br/>SSE chunks
```

---

## 3. Class Diagram

```mermaid
classDiagram
    class ChatService {
        +FoundryAgentService foundry_service
        +HybridSessionService session_service
        -str _agent_id
        +get_agent_id() str
        +get_or_create_thread(session_id) str
        +process_chat_request_with_files(session_id, query, file_ids) Dict
    }

    class FoundryAgentService {
        +AIProjectClient client
        +str model
        +str agent_id
        +bool enable_blob_storage
        +BlobStorageService blob_service
        +bool disable_local_files
        +HybridSessionService session_service
        -str _base_agent
        -str _current_session_id
        +upload_file(file_path) str
        +upload_file_from_bytes(file_content, filename) str
        +upload_document_from_url(url) str
        +get_or_create_base_agent() str
        +create_thread() str
        +create_message(thread_id, content, file_ids) str
        +run_thread(thread_id, agent_id) Dict
        +get_latest_message(thread_id, run_id, session_id) List~Dict~
        -_get_content_type_from_filename(filename) str
        -_detect_image_ext(data) Optional~str~
        -_infer_ext_and_content_type(data, default_ext, hint_text) Tuple
        -_capture_run_artifacts(thread_id, run_id, session_id, images_dir, response_content)
    }

    class HybridSessionService {
        +CosmosSessionService cosmos_service
        +get_thread_id(session_id) Optional~str~
        +save_session(session_id, thread_id) None
        +log_interaction(session_id, thread_id, query, response, file_ids, execution_time) None
    }

    class CosmosSessionService {
        +str cosmos_endpoint
        +str database_name
        +str container_name
        +CosmosClient cosmos_client
        +container
        +container_files
        +container_deletion
        +get_thread_id(session_id) Optional~str~
        +save_session(session_id, thread_id) None
        +add_uploaded_file(session_id, file_id, filename) None
        +add_existing_files(session_id, file_ids) None
        +add_generated_file(session_id, file_id, file_type, filename) None
        +get_session_files(session_id) Dict
        +log_interaction(session_id, thread_id, query, response, file_ids, execution_time) None
    }

    class BlobStorageService {
        +str account_url
        +str container_name
        +DefaultAzureCredential credential
        +BlobServiceClient blob_service_client
        +container_client
        +upload_file(session_id, file_content, filename, content_type, metadata, subfolder) Dict
        +generate_sas_url(blob_path, expiry_hours) str
        +list_session_files(session_id, date) list
        +get_blob_info(blob_path) Optional~Dict~
        +get_subfolder_for_file_type(filename, file_source) str
        -_ensure_container_exists()
        -_generate_blob_path(session_id, filename, date, subfolder) str
        -_get_content_type(filename) str
    }

    class BlobOperations_ {
        +str container_name
        +int max_workers
        +BlobServiceClient _client
        +container
        +extract_blob_path(url, container_name) str
        +read_blob(blob_path) Tuple~str_bytes~
        +close()
    }

    class SessionService {
        +str excel_file
        +get_thread_id(session_id) Optional~str~
        -_ensure_excel_file_exists()
    }

    ChatService --> FoundryAgentService : foundry_service
    ChatService --> HybridSessionService : session_service
    HybridSessionService --> CosmosSessionService : cosmos_service
    FoundryAgentService --> BlobStorageService : blob_service
    FoundryAgentService --> HybridSessionService : session_service
```

---

## 4. File Processing & Artifact Classification Flow

```mermaid
flowchart TD
    START["Agent Run Completed<br/>get_latest_message()"] --> LIST["List messages<br/>agents.messages.list(thread_id, limit=1)"]
    LIST --> LOOP["For each content item<br/>in latest_message.content"]

    LOOP --> TEXT{"content.type?"}

    TEXT -->|"text"| EXTRACT_TEXT["Extract text value<br/>→ response_content[type=text]"]
    EXTRACT_TEXT --> ANNOTATIONS{"Has file_path<br/>annotations?"}

    ANNOTATIONS -->|"Yes"| ANN_LOOP["For each annotation"]
    ANNOTATIONS -->|"No"| NEXT["Next content item"]

    ANN_LOOP --> GET_FID["Extract file_id from<br/>annotation.file_path.file_id"]
    GET_FID --> DOWNLOAD_ANN["Download file bytes<br/>agents.files.get_content(file_id)"]
    DOWNLOAD_ANN --> DETECT_EXT["Detect extension from<br/>sandbox_path or infer"]

    DETECT_EXT --> INFER{"Extension == .png<br/>(default)?"}
    INFER -->|"Yes"| SMART_INFER["_infer_ext_and_content_type()<br/>Check magic bytes:<br/>PNG? JPG? GIF? SVG?<br/>XLSX (PK header)?<br/>CSV? JSON? TXT?"]
    INFER -->|"No"| USE_EXT["Use detected extension"]

    SMART_INFER --> CLASSIFY
    USE_EXT --> CLASSIFY

    CLASSIFY{"File type?"}
    CLASSIFY -->|".png/.jpg/.svg"| IMG_TYPE["type = 'image'<br/>subfolder = 'imgs'"]
    CLASSIFY -->|".csv/.xlsx/.json/.txt"| FILE_TYPE["type = 'file_output'<br/>subfolder = 'files-gen'"]

    IMG_TYPE --> UPLOAD_BLOB["Upload to Blob Storage<br/>blob_service.upload_file()"]
    FILE_TYPE --> UPLOAD_BLOB

    UPLOAD_BLOB --> TRACK_COSMOS["Track in Cosmos DB<br/>add_generated_file()<br/>add_existing_files()"]
    TRACK_COSMOS --> APPEND["Append to response_content<br/>{type, file_id, blob_url, filename}"]
    APPEND --> NEXT

    TEXT -->|"image_file"| IMG_FILE["Extract file_id from<br/>content.image_file.file_id"]
    IMG_FILE --> DOWNLOAD_IMG["Download image bytes<br/>agents.files.get_content(file_id)"]
    DOWNLOAD_IMG --> SAVE_LOCAL{"Local storage<br/>enabled?"}
    SAVE_LOCAL -->|"Yes"| WRITE_LOCAL["Write to images/ dir"]
    SAVE_LOCAL -->|"No"| SKIP_LOCAL["Skip local write"]
    WRITE_LOCAL --> BLOB_ENABLED{"Blob storage<br/>enabled?"}
    SKIP_LOCAL --> BLOB_ENABLED
    BLOB_ENABLED -->|"Yes"| UPLOAD_IMG["Upload to blob<br/>subfolder='imgs'"]
    BLOB_ENABLED -->|"No"| LOCAL_ONLY["Add local path to response"]
    UPLOAD_IMG --> TRACK_IMG["Track in Cosmos DB"]
    TRACK_IMG --> APPEND_IMG["Append to response_content<br/>{type=image, blob_url, ...}"]
    APPEND_IMG --> NEXT
    LOCAL_ONLY --> NEXT
```

---

## 5. Session Management Flow

```mermaid
flowchart TD
    REQ["Incoming Request<br/>session_id from body"] --> GET["ChatService.get_or_create_thread(session_id)"]
    GET --> HYBRID["HybridSessionService.get_thread_id(session_id)"]
    HYBRID --> COSMOS_Q["CosmosSessionService.get_thread_id(session_id)"]

    COSMOS_Q --> RETRY_LOOP{"Attempt 1..3<br/>(with exponential backoff)"}
    RETRY_LOOP --> QUERY["Query Cosmos DB:<br/>SELECT thread_id<br/>WHERE session_id = @sid<br/>ORDER BY _ts DESC"]

    QUERY --> FOUND{"thread_id<br/>found?"}
    FOUND -->|"Yes"| RETURN_TID["Return existing thread_id"]
    FOUND -->|"No"| CREATE_NEW["Create new thread"]

    CREATE_NEW --> AI_THREAD["FoundryAgentService.create_thread()<br/>→ agents.threads.create()"]
    AI_THREAD --> SAVE["HybridSessionService.save_session()"]
    SAVE --> COSMOS_SAVE["CosmosSessionService.save_session()"]

    COSMOS_SAVE --> UPSERT["Upsert to Cosmos DB:<br/>{id, session_id, thread_id,<br/>uploaded_files: [],<br/>generated_files: [],<br/>type: 'session_mapping'}"]

    RETRY_LOOP -->|"Connection error"| BACKOFF["Sleep (1s, 2s, 4s)<br/>Exponential backoff"]
    BACKOFF --> RETRY_LOOP
    RETRY_LOOP -->|"All attempts failed"| RETURN_NONE["Return None<br/>→ triggers new thread creation"]

    RETURN_TID --> PROCEED["Proceed with chat processing"]
    UPSERT --> PROCEED

    subgraph "Cosmos DB Containers"
        C1["sessions container<br/>(session_mapping docs)"]
        C2["files container<br/>(existing_files docs)"]
        C3["deletion container<br/>(deletion job docs)"]
    end

    UPSERT --> C1
```

---

## 6. Blob Storage Organization

```mermaid
graph TD
    subgraph "Azure Blob Storage Container<br/>(data-analyst-session-files)"
        subgraph "session-abc-123/"
            subgraph "user-uploads/"
                UF1["report.csv"]
                UF2["data.xlsx"]
            end
            subgraph "imgs/"
                IF1["file-id-001.png"]
                IF2["file-id-002.jpg"]
            end
            subgraph "files-gen/"
                GF1["file-id-003.csv"]
                GF2["file-id-004.xlsx"]
                GF3["file-id-005.json"]
            end
        end
        subgraph "session-def-456/"
            subgraph "user-uploads/ "
                UF3["sales.csv"]
            end
            subgraph "imgs/ "
                IF3["file-id-006.png"]
            end
            subgraph "files-gen/ "
                GF4["file-id-007.txt"]
            end
        end
    end

    UPLOAD["upload_file()"] -->|"subfolder param"| ROUTE{"Subfolder?"}
    ROUTE -->|"user-uploads"| UF1
    ROUTE -->|"imgs"| IF1
    ROUTE -->|"files-gen"| GF1

    SAS["generate_sas_url()"] -->|"User delegation key<br/>24h expiry, read-only"| SASURL["https://...blob.core.../path?sv=...&sig=..."]
```

---

## 7. Streaming Response Format (SSE)

```mermaid
sequenceDiagram
    participant FA as Function App
    participant C as Client

    Note over FA: _create_response(result)
    FA->>FA: Check result.status == "completed"

    Note over FA: _stream_response(result)

    FA->>C: [Text Content]<br/>"Here is your analysis...\n\n"

    Note over FA: Build attachments array
    FA->>FA: For each image/file_output in response:<br/>Build {type, fileId, fileName, url, metadata}

    FA->>C: event: attachments<br/>[{"type":"png","fileId":"...","fileName":"chart.png","url":"https://sas-url...","metadata":{...}}]

    Note over C: Client parses SSE events:<br/>1. Default text → display as message<br/>2. "attachments" event → render images/download links

    alt Error occurred
        Note over FA: _stream_error(result)
        FA->>C: event: error<br/>{"error":"...","message":"...","status":"error"}
    end
```

---

## 8. Telemetry & Observability Flow

```mermaid
flowchart LR
    subgraph "Application Code"
        FA["function_app.py"]
        MOD["models.py"]
        BS["blob_service.py"]
    end

    subgraph "telemetry.py"
        CS_SPAN["create_custom_span()"]
        ADD_ATTR["add_span_attributes()"]
        REC_EXC["record_exception()"]
        CORR["create_correlation_context()"]
        CORR_SCOPE["CorrelationScope"]
    end

    subgraph "OpenTelemetry SDK"
        TRACER["Tracer<br/>(clippy-data-detective)"]
        METER["Meter<br/>(clippy-data-detective)"]
        COUNTER["Counter:<br/>data_analyst_requests"]
        HIST["Histogram:<br/>data_analyst_latency_ms"]
    end

    subgraph "Azure Monitor"
        APPINS["Application Insights"]
    end

    FA -->|"TRACER.start_as_current_span"| TRACER
    FA -->|"REQUEST_COUNTER.add(1)"| COUNTER
    FA -->|"LATENCY_HIST.record(ms)"| HIST
    MOD -->|"create_custom_span()"| CS_SPAN
    MOD -->|"add_span_attributes()"| ADD_ATTR
    MOD -->|"record_exception()"| REC_EXC
    MOD -->|"create_correlation_context()"| CORR
    CORR --> CORR_SCOPE
    CS_SPAN --> TRACER
    TRACER --> APPINS
    METER --> APPINS
    COUNTER --> METER
    HIST --> METER

    subgraph "Span Hierarchy"
        S1["data_analyst (root)"]
        S2["  └─ correlation_scope"]
        S3["      ├─ foundry_upload_file_from_bytes"]
        S4["      ├─ cosmos_get_thread_id"]
        S5["      ├─ cosmos_save_session"]
        S6["      ├─ foundry_create_message"]
        S7["      ├─ foundry_run_thread"]
        S8["      └─ foundry_upload_file"]
    end
```

---

## 9. Attachment Upload Pipeline (Detailed)

```mermaid
flowchart TD
    INPUT["Client sends attachments[]<br/>{url, fileName, size}"]
    INPUT --> LOOP["For each attachment"]

    LOOP --> READ["BlobOperations_.read_blob(url)"]
    READ --> EXTRACT{"URL starts with http?"}
    EXTRACT -->|"Yes"| PARSE["extract_blob_path(url, container_name)<br/>Parse container path from URL"]
    EXTRACT -->|"No"| DIRECT["Use path directly"]
    PARSE --> DL["Download blob via async client<br/>blob_client.download_blob()"]
    DIRECT --> DL
    DL --> BYTES["Return (path, bytes)"]

    BYTES --> FOUNDRY_UP["FoundryAgentService.upload_file_from_bytes(bytes, fileName)"]

    FOUNDRY_UP --> BLOB_CHECK{"STORE_USER_FILES_IN_BLOB<br/>== true?"}
    BLOB_CHECK -->|"Yes"| USER_BLOB["Upload to blob storage<br/>subfolder='user-uploads'<br/>with metadata"]
    BLOB_CHECK -->|"No"| SKIP_BLOB["Skip blob storage"]

    USER_BLOB --> TEMP["Write to temp file"]
    SKIP_BLOB --> TEMP

    TEMP --> AI_UPLOAD["upload_file(temp_path)<br/>→ agents.files.upload_and_poll()<br/>purpose=AGENTS"]
    AI_UPLOAD --> CLEANUP["Delete temp file"]
    CLEANUP --> RETURN_FID["Return file_id"]

    RETURN_FID --> COLLECT["Collect all file_ids"]
    COLLECT --> CHAT["Pass to process_chat_request_with_files()"]
```

---

## 10. Cosmos DB Document Schema

```mermaid
erDiagram
    SESSION_MAPPING {
        string id PK "session_id + timestamp"
        string session_id "Partition key candidate"
        string thread_id "Azure AI Foundry thread ID"
        array uploaded_files "User uploaded file tracking"
        array generated_files "AI generated file tracking"
        string created_at "ISO timestamp"
        string last_updated "ISO timestamp"
        string type "session_mapping"
    }

    UPLOADED_FILE_ENTRY {
        string file_id "Azure AI Foundry file ID"
        string filename "Original filename"
        string uploaded_at "ISO timestamp"
    }

    GENERATED_FILE_ENTRY {
        string file_id "Azure AI Foundry file ID"
        string filename "Generated filename"
        string file_type "image or file_output"
        string generated_at "ISO timestamp"
    }

    EXISTING_FILES_DOC {
        string id PK "session_id + timestamp"
        string session_id
        string file_ids "File IDs for deletion job"
        string added_at "ISO timestamp"
        string type "existing_files"
    }

    INTERACTION_LOG {
        string id PK "session_id + thread_id + timestamp"
        string session_id
        string thread_id
        string timestamp "ISO timestamp"
        string query "User query text"
        array response "Response content array"
        array file_ids "Associated file IDs"
        number execution_time_seconds
        number response_length
        string type "interaction_log"
    }

    SESSION_MAPPING ||--o{ UPLOADED_FILE_ENTRY : "uploaded_files[]"
    SESSION_MAPPING ||--o{ GENERATED_FILE_ENTRY : "generated_files[]"
    SESSION_MAPPING ||--o{ INTERACTION_LOG : "session_id"
    SESSION_MAPPING ||--o{ EXISTING_FILES_DOC : "session_id"
```

---

## 11. End-to-End Data Flow Summary

```mermaid
flowchart LR
    A["👤 User"] -->|"1. Send query + files"| B["⚡ Azure Function"]
    B -->|"2. Download attachments"| C["📦 External Blob"]
    B -->|"3. Upload to AI"| D["🤖 Azure AI Foundry"]
    B -->|"4. Lookup/Save session"| E["💾 Cosmos DB"]
    D -->|"5. Agent runs code<br/>interpreter, generates<br/>text + charts + files"| D
    D -->|"6. Return results"| B
    B -->|"7. Store artifacts"| F["☁️ Blob Storage"]
    B -->|"8. Track files"| E
    B -->|"9. Stream SSE response<br/>text + attachment URLs"| A
    B -.->|"10. Telemetry"| G["📈 App Insights"]
```
