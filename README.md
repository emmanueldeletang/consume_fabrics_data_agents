# Fabric Data Agent Web Application

A Flask-based web application that provides a conversational interface to Microsoft Fabric Data Agents using the OpenAI Assistants API. Includes chat history persistence via MongoDB and multi-agent support with tenant isolation.

## Features

- **Multi-Agent Support**: Switch between different Fabric Data Agents at runtime
- **Assistants API Integration**: Uses OpenAI Assistants API (threads/runs) for reliable agent communication
- **Chat History**: Persistent conversation storage in MongoDB
- **Azure Authentication**: Passwordless authentication via Azure Identity (ClientSecretCredential)
- **Tenant Isolation**: Support for generic agents with per-tenant data filtering
- **Usage Tracking**: Monitor token consumption and Capacity Units (CU) minutes
- **Performance Metrics**: Track query creation, execution, and round-trip times
- **Web UI**: Interactive HTML interface for chatting with agents

## Architecture

### API Flow (per user message)

```
1. POST /assistants           → create transient assistant
2. POST /threads              → create thread for conversation
3. POST /threads/{id}/messages → add user message
4. POST /threads/{id}/runs    → trigger the agent
5. GET /threads/{id}/runs/{run_id} → poll until terminal state
6. GET /threads/{id}/messages → read agent reply
7. DELETE /threads/{id}       → clean up thread
8. DELETE /assistants/{id}    → clean up assistant
```

All requests include `?api-version=2024-05-01-preview` and an `ActivityId` header for tracing.

### Components

- **app.py**: Main Flask application with Assistants API integration
- **app_config.py**: Configuration loader (agents, credentials, endpoints)
- **requirements.txt**: Python dependencies
- **templates/index.html**: Web UI for chatting
- **config/agents.json**: Agent registry and definitions

## Prerequisites

- **Python 3.8+**
- **MongoDB**: For chat history persistence (URI via `MONGO_URI` env var)
- **Azure Fabric Workspace**: With deployed Data Agents
- **Azure Service Principal**: For authentication
  - Tenant ID
  - Client ID
  - Client Secret
- **Fabric Scope**: `https://fabric.microsoft.com/.default`

## Setup

### 1. Clone & Install Dependencies

```bash
cd fabricksavezedo
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Create a `.env` file in the project root:

```env
# Azure
TENANT_ID=<your-tenant-id>
ADMIN_CLIENT_ID=<your-service-principal-client-id>
ADMIN_CLIENT_SECRET=<your-service-principal-secret>

# MongoDB
MONGO_URI=mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority

# Fabric API
FABRIC_SCOPE=https://fabric.microsoft.com/.default
API_VERSION=2024-05-01-preview
```

### 3. Configure Agents

Edit `config/agents.json` to define your agents:

```json
{
  "adventure_works": {
    "name": "AdventureWorks Analytics",
    "description": "Query AdventureWorks data",
    "base_url": "https://<fabric-workspace>/data-agents/<agent-id>",
    "capabilities": ["SQL Query", "Data Analysis"],
    "sample_questions": [
      "What are our top 10 customers?",
      "Show sales by region"
    ]
  },
  "generic_agent": {
    "name": "Tenant-Aware Agent",
    "description": "Query any tenant's data with isolation",
    "base_url": "https://<fabric-workspace>/data-agents/<generic-agent-id>",
    "capabilities": ["Multi-tenant", "Secure Access"],
    "sample_questions": []
  }
}
```

## Running the Application

```bash
python app.py
```

The app will:
1. Authenticate with Azure (opens browser once for interactive login, then caches token)
2. Connect to MongoDB (with warning if unavailable—chat history won't persist)
3. Start Flask server at **http://localhost:5000**

## API Endpoints

### GET `/`
Serves the web UI (`templates/index.html`).

### GET `/agents`
Returns available agents and their metadata.

**Response:**
```json
{
  "adventure_works": {
    "name": "AdventureWorks Analytics",
    "description": "...",
    "capabilities": [...],
    "sample_questions": [...]
  }
}
```

### POST `/chat`
Send a message to an agent and get a response.

**Request:**
```json
{
  "message": "What are our top 10 customers?",
  "agent_key": "adventure_works",
  "tenant": "acme-corp"
}
```

**Response:**
```json
{
  "reply": "Based on sales data, the top 10 customers are...",
  "messages": [
    {"role": "user", "content": "What are our top 10 customers?"},
    {"role": "assistant", "content": "Based on sales data..."}
  ],
  "usage": {
    "prompt_tokens": 450,
    "completion_tokens": 280,
    "total_tokens": 730,
    "cu_minutes": 0.0547
  },
  "timings": {
    "query_creation_seconds": 0.23,
    "agent_call_seconds": 1.45,
    "query_execution_seconds": 2.18,
    "total_request_seconds": 3.92,
    "started_at": "2026-06-18T10:30:00+00:00",
    "finished_at": "2026-06-18T10:30:03.92+00:00"
  }
}
```

### GET `/history/<agent_key>`
Retrieve conversation history for a specific agent.

**Response:**
```json
{
  "messages": [
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
```

### DELETE `/history/<agent_key>`
Clear conversation history for a specific agent.

**Response:**
```json
{"ok": true}
```

## Configuration Details

### Agent Configuration (config/agents.json)

Each agent entry requires:
- **name**: Display name
- **description**: Brief description
- **base_url**: Fabric Data Agent endpoint
- **capabilities**: List of agent capabilities
- **sample_questions**: Example questions for the UI

### Tenant Isolation (generic_agent)

For multi-tenant scenarios, the app automatically:
1. Prepends tenant context to user messages
2. Injects SQL filtering rules (e.g., `WHERE customer_company = '{tenant}'`)
3. Enforces isolation rules to prevent cross-tenant data access
4. Tags responses with tenant context

Example injected rule:
```
You are the NorthWind Insights assistant for tenant acme-corp.

ABSOLUTE RULES:
1. Every SQL query MUST include: customer_company = 'acme-corp'
2. If asked about other companies, refuse with: "I can only provide data for acme-corp"
3. End responses with: "Source: NorthWind Lakehouse — tenant acme-corp"
```

## Usage Tracking

The app tracks two key metrics:

### Token Usage
- **prompt_tokens**: Tokens in user message + context
- **completion_tokens**: Tokens in agent response
- **total_tokens**: Sum of prompt + completion
- **cu_minutes**: Estimated Capacity Units consumed (billing metric)

**CU Calculation:**
```
cu_minutes = (prompt_tokens × 100 + completion_tokens × 400) / 1000 / 60
```

### Performance Timings
- **query_creation_seconds**: Time to format and send user message
- **agent_call_seconds**: Time to invoke the agent
- **query_execution_seconds**: Time agent spends processing (polling)
- **total_request_seconds**: End-to-end round trip

All timings are logged to console:
```
[USAGE] agent=adventure_works prompt_tokens=450 completion_tokens=280 total_tokens=730 cu_minutes=0.0547
[TIMING] agent=adventure_works query_create_s=0.23 agent_call_s=1.45 query_exec_s=2.18 total_s=3.92
```

## Troubleshooting

### Authentication Fails
- Verify `TENANT_ID`, `ADMIN_CLIENT_ID`, and `ADMIN_CLIENT_SECRET` in `.env`
- Ensure the service principal has `Fabric.Admin` role in the workspace

### MongoDB Connection Fails
- Check `MONGO_URI` connection string
- Verify network access (IP whitelist, VPN, etc.)
- App warns but continues—chat history simply won't persist

### Agent Returns No Response
- Verify agent endpoint in `config/agents.json`
- Check agent has access to data in the lakehouse
- Look for `last_error` in response (check logs for HTTP 502 errors)

### Timeout Errors
- Increase `timeout_s` parameter in `_ask()` (default: 300s)
- May indicate slow query execution in agent backend

## Development

### Running Tests
```bash
# (Add pytest tests as needed)
pytest tests/
```

### Debugging
Enable Flask debug mode:
```python
app.run(host="127.0.0.1", port=5000, debug=True)
```

### Logging
Check console output for `[USAGE]` and `[TIMING]` lines for each request.

## Related Components

- **go-fabric-agent/**: Go-based companion agent service
- **deploy_adventureworks_agent.py**: Deployment script for AdventureWorks agent
- **deploy_dataagent.py**: Deployment script for data agents
- **load_data.py**: Data loading utilities
- **setup_fabric_eventstream.py**: EventStream setup
- **send_kafka_events.py**: Kafka event producer

## License

[Specify your license here]

## Support

For issues, errors, or feature requests, please refer to:
- [Microsoft Fabric Documentation](https://learn.microsoft.com/fabric/)
- [OpenAI Assistants API Reference](https://platform.openai.com/docs/assistants)
- Project repository issues

---

**Last Updated**: June 18, 2026
