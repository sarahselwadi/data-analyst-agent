# Implementation Plan

[Overview]
Capture all Code Interpreter tool invocations (input code, stdout/stderr logs, and image file_ids) from Azure AI Foundry run steps and persist them in a new dedicated Cosmos DB container immediately after each agent run completes.

The Azure AI Foundry Agents API exposes Code Interpreter invocations through run step tool calls. After a run completes via `create_and_process()`, we can call `run_steps.list(thread_id, run_id)` to iterate over each step. For steps of type `tool_calls`, we inspect each tool call — when `type == "code_interpreter"`, we extract `code_interpreter.input` (the actual Python code the model executed) and `code_interpreter.outputs[]` (which contains `logs` entries with stdout/stderr text and `image` entries with `file_id` references).

This feature adds a new Cosmos DB container called `code_executions` (configurable via `COSMOS_CONTAINER_NAME_CODE` env var, partition key `/session_id`). Each Code Interpreter invocation becomes its own document, enabling unbounded growth per session, independent querying, and clean separation from session mapping data. The container is auto-created on startup using the same pattern as the existing `artifact_registry` container.

The capture happens synchronously right after `run_thread()` completes — no real-time polling. Image outputs from code interpreter are NOT re-uploaded to blob storage (the existing message-level pipeline already handles that). We only log the code, logs, and image file_id references to Cosmos.

[Types]
One new Cosmos DB document schema for code execution records.

### Code Execution Document Schema

```json
{
  "id": "{run_id}_{step_id}_{call_index}_{timestamp_ms}",
  "session_id": "string — conversation/session ID (PARTITION KEY)",
  "thread_id": "string — Azure AI Foundry thread ID",
  "run_id": "string — Azure AI Foundry run ID",
  "step_id": "string — run step ID",
  "call_index": "int — index of the tool call within the step (0-based)",
  "code_input": "string — the Python code the model sent to Code Interpreter",
  "outputs": [
    {
      "type": "logs",
      "logs": "string — stdout/stderr text output"
    },
    {
      "type": "image",
      "file_id": "string — Azure AI Foundry file ID for the generated image"
    }
  ],
  "status": "string — step status (e.g., 'completed', 'failed')",
  "created_at": "string — ISO 8601 timestamp",
  "date": "string — YYYY-MM-DD for potential date-based queries"
}
```

**Field details:**
- `id`: Composite key ensuring uniqueness: `{run_id}_{step_id}_{call_index}_{timestamp_ms}`
- `session_id`: Partition key. Matches the session_id used throughout the app.
- `thread_id`: For cross-referencing with the `threadssessions` container.
- `run_id`: The specific agent run that produced this code execution.
- `step_id`: The run step ID within the run.
- `call_index`: When a single step has multiple tool calls, this differentiates them.
- `code_input`: The raw Python code string the model fed to Code Interpreter. Can be multi-line.
- `outputs`: Array of output objects. Each has a `type` field:
  - `"logs"`: Contains a `logs` string with stdout/stderr text.
  - `"image"`: Contains a `file_id` string referencing the generated image in AI Foundry.
- `status`: The status of the run step (from `step.status`).
- `created_at`: When this document was written.
- `date`: Date string for potential future date-partitioned queries or cleanup jobs.

[Files]
Three existing files modified, no new files created.

### Files to Modify

1. **`models.py`** — Primary changes:
   - `CosmosSessionService.__init__()`: Add initialization of `code_executions` container (env var `COSMOS_CONTAINER_NAME_CODE`, auto-create with partition key `/session_id`)
   - `CosmosSessionService`: Add new method `log_code_execution()`
   - `FoundryAgentService`: Add new method `capture_code_interpreter_runs()`
   - `FoundryAgentService.run_thread()`: Add call to `capture_code_interpreter_runs()` after `create_and_process()` returns

2. **`.env.example`** — Add `COSMOS_CONTAINER_NAME_CODE` environment variable with default value and documentation comment

3. **`architecture_diagram.md`** — Update Cosmos DB document schema diagram (Section 10) to include the new `code_executions` container/document type

### Files NOT Modified
- `function_app.py` — No changes needed. The capture is internal to `run_thread()`.
- `BlobOperations.py` — No changes needed. Image uploads already handled by existing pipeline.
- `telemetry.py` — No changes needed. Existing `create_custom_span` used as-is.
- `requirements.txt` — No new dependencies needed.

[Functions]
Two new functions added, one existing function modified.

### New Functions

1. **`CosmosSessionService.log_code_execution()`**
   - **File**: `models.py`
   - **Signature**: `def log_code_execution(self, session_id: str, thread_id: str, run_id: str, step_id: str, call_index: int, code_input: str, outputs: List[Dict[str, Any]], status: str = "completed") -> None`
   - **Purpose**: Writes a single code execution document to the `code_executions` Cosmos container. Each call to this method logs one Code Interpreter tool call invocation.
   - **Error handling**: Catches and logs exceptions without re-raising (same pattern as `log_artifact()`), so code execution logging failures don't break the main agent flow.
   - **Implementation details**:
     - Constructs the document ID as `f"{run_id}_{step_id}_{call_index}_{int(now.timestamp() * 1000)}"`
     - Sets `date` to `now.strftime("%Y-%m-%d")`
     - Calls `self.container_code_executions.upsert_item(doc)`
     - Logs success/failure with appropriate emoji prefixes

2. **`FoundryAgentService.capture_code_interpreter_runs()`**
   - **File**: `models.py`
   - **Signature**: `def capture_code_interpreter_runs(self, thread_id: str, run_id: str, session_id: str) -> None`
   - **Purpose**: Lists all run steps for a completed run, finds Code Interpreter tool calls, extracts input/outputs, and logs each to Cosmos via `session_service.cosmos_service.log_code_execution()`.
   - **Implementation details**:
     - Calls `self.client.agents.run_steps.list(thread_id=thread_id, run_id=run_id)`
     - Iterates over steps; for each step, accesses `step.step_details` to find `tool_calls`
     - For each tool call where `type == "code_interpreter"`, extracts:
       - `code_interpreter.input` → `code_input`
       - `code_interpreter.outputs` → list of `{type, logs}` or `{type, file_id}` dicts
     - Calls `log_code_execution()` for each code interpreter tool call
     - Wrapped in `create_custom_span("capture_code_interpreter_runs", "ai_foundry")`
     - Uses defensive attribute access (`getattr`) to handle SDK shape variations
     - Entire method is wrapped in try/except — failures are logged but don't break the run flow
     - Logs summary: "Captured N code interpreter executions from M run steps"

### Modified Functions

1. **`FoundryAgentService.run_thread()`**
   - **File**: `models.py`
   - **Current location**: Around line 230 in `models.py`
   - **Change**: After the `run = self.client.agents.runs.create_and_process(...)` call and after logging the run status, add a call to `self.capture_code_interpreter_runs(thread_id, run.id, session_id)`.
   - **Signature change**: Add `session_id: str = None` parameter to the method signature (currently only takes `thread_id` and `agent_id`).
   - **Cascading change**: Update the call site in `ChatService.process_chat_request_with_files()` to pass `session_id` to `run_thread()`.

[Classes]
Two existing classes modified, no new classes.

### Modified Classes

1. **`CosmosSessionService`** (in `models.py`)
   - **New instance variable**: `self.container_code_executions` — Cosmos container client for the `code_executions` container
   - **New instance variable**: `self.container_name_code_executions` — Container name from env var
   - **`__init__()` changes**: 
     - Read `COSMOS_CONTAINER_NAME_CODE` env var (default: `"code_executions"`)
     - Call `self.database.create_container_if_not_exists(id=..., partition_key=PartitionKey(path="/session_id"))` — same pattern as artifact_registry
     - Get container client: `self.container_code_executions = self.database.get_container_client(self.container_name_code_executions)`
     - Add logging for the new container
   - **New method**: `log_code_execution()` (see Functions section)

2. **`FoundryAgentService`** (in `models.py`)
   - **New method**: `capture_code_interpreter_runs()` (see Functions section)
   - **Modified method**: `run_thread()` — new `session_id` parameter, call to `capture_code_interpreter_runs()` after run completes

3. **`ChatService`** (in `models.py`)
   - **Modified method**: `process_chat_request_with_files()` — pass `session_id` to `self.foundry_service.run_thread(thread_id, self.get_agent_id(), session_id=session_id)` (was previously not passing session_id)

[Dependencies]
No new dependencies required.

All required packages are already in `requirements.txt`:
- `azure-ai-agents` — provides `run_steps.list()` API
- `azure-ai-projects` — provides `AIProjectClient`
- `azure-cosmos` — provides Cosmos DB client and `PartitionKey`

No version changes needed.

[Testing]
Manual testing approach via the existing test infrastructure.

### Testing Strategy

1. **Unit verification**: After implementation, trigger a chat request via `test.html` or direct POST to `/api/data-analyst` with a query that forces code execution (e.g., "create a simple bar chart" or "calculate the sum of 1 to 100 using Python").

2. **Cosmos DB verification**: After the request completes, query the `code_executions` container in Azure Portal or via Cosmos DB Data Explorer:
   ```sql
   SELECT * FROM c WHERE c.session_id = '<your-session-id>' ORDER BY c.created_at DESC
   ```
   Verify that:
   - Documents exist with the correct `session_id`, `thread_id`, `run_id`
   - `code_input` contains the actual Python code the agent ran
   - `outputs` array contains `logs` entries with stdout/stderr text
   - `outputs` array contains `image` entries with valid `file_id` values (when charts are generated)

3. **Error resilience**: Verify that if the `code_executions` container is unavailable or the `run_steps.list()` call fails, the main chat flow still completes successfully (errors are logged but not raised).

4. **Multi-step verification**: Send a complex query that triggers multiple code interpreter invocations in a single run. Verify that each invocation gets its own document with the correct `call_index`.

5. **No regression**: Verify that existing functionality (file uploads, image generation, session management, artifact registry) continues to work unchanged.

[Implementation Order]
Five sequential steps to implement the feature with minimal risk.

1. **Step 1: Update `.env.example`** — Add `COSMOS_CONTAINER_NAME_CODE` environment variable with documentation. This is a standalone change with no code dependencies.

2. **Step 2: Update `CosmosSessionService.__init__()`** — Add container initialization for `code_executions` (read env var, auto-create container, get client). Follow the exact same pattern as `artifact_registry` container init.

3. **Step 3: Add `CosmosSessionService.log_code_execution()`** — Implement the new method that writes code execution documents to Cosmos. This can be tested independently.

4. **Step 4: Add `FoundryAgentService.capture_code_interpreter_runs()`** — Implement the run steps parsing method. This reads from the AI Foundry API and calls `log_code_execution()`.

5. **Step 5: Modify `FoundryAgentService.run_thread()` and `ChatService.process_chat_request_with_files()`** — Add `session_id` parameter to `run_thread()`, call `capture_code_interpreter_runs()` after run completes, and update the call site in `ChatService` to pass `session_id`. This is the integration step that wires everything together.

6. **Step 6: Update `architecture_diagram.md`** — Add the new `code_executions` container to the Cosmos DB schema diagram (Section 10) to keep documentation in sync.
