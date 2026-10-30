# Forge — AI Project Builder

A full-stack project-generation workspace. Users submit a topic or upload a brief, ask a configurable AI provider to generate a codebase, preview files, download a ZIP, and publish to GitHub using OAuth.

## Architecture

- **Frontend:** React 19, Vite 6, plain CSS.
- **Backend:** FastAPI, Python 3.10+.
- **AI:** OpenAI-compatible Chat Completions endpoint configured server-side.
- **GitHub:** OAuth web flow and GitHub REST API. OAuth token is held in backend process memory and is not sent to the browser.
- **Uploads:** PDF (PyMuPDF), DOCX (python-docx), TXT; image OCR (Pillow + Tesseract).

## Requirements

- Python 3.10+
- Node.js 20+
- AI provider API key and a compatible Chat Completions API.
- For GitHub publishing: a GitHub OAuth App with callback URL `http://localhost:8000/api/github/callback`.
- For image OCR: Tesseract OCR installed on the host OS.

## Run locally

### 1. Backend

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `backend/.env`:
- `AI_API_KEY`: provider key (required for analyze/generate).
- `AI_BASE_URL`: API base URL, default OpenAI.
- `AI_MODEL`: model name supported by the provider.
- `SESSION_SECRET`: replace with a long random secret.
- `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`: OAuth app credentials.
- `GITHUB_CALLBACK_URL`: must exactly match the callback configured in GitHub.
- `COOKIE_SECURE=true` when deployed behind HTTPS.
- Set `CORS_ORIGINS` to the exact frontend origin(s).

Start the API:

```bash
uvicorn app.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs

### 2. Frontend

In a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. Optionally create `frontend/.env` with `VITE_API_URL=http://localhost:8000`.

## GitHub OAuth setup

1. Create a GitHub OAuth App in GitHub Developer Settings.
2. Set the authorization callback URL to `http://localhost:8000/api/github/callback`.
3. Add client ID and secret to backend `.env`.
4. The app requests the classic `repo` OAuth scope because it supports both private repositories and creating/updating contents. For a production deployment, prefer a GitHub App installation flow with narrowly scoped Contents read/write and Metadata read permissions.
5. Use HTTPS in production. OAuth state is validated and session cookies are signed.

## API

- `POST /api/projects/analyze` — analyze topic/stack using configured AI.
- `POST /api/projects/generate` — multipart topic, stack, optional `file`.
- `GET /api/projects/{project_id}/files` — list generated files.
- `GET /api/projects/{project_id}/file?path=...` — preview a file.
- `GET /api/projects/{project_id}/download` — download ZIP.
- `GET /api/github/login` — begin OAuth.
- `GET /api/github/callback` — validate state and exchange code.
- `GET /api/github/status` — check session connection.
- `GET /api/github/repositories` — list authenticated user's repositories.
- `POST /api/github/publish` — create or update repository contents.

## Security and operational notes

- Never put AI or GitHub secrets in frontend environment variables. `VITE_*` values are public.
- Uploads are limited to 10 MB; PDF/DOCX/TXT and common image formats are accepted. OCR requires system Tesseract.
- File paths are validated; generated ZIP excludes `.env`, credentials, dependency directories, caches, build output, and internal ownership marker.
- GitHub publishing requires explicit confirmation in the UI and API (`confirm: true`). Existing files are updated using their current Git blob SHA.
- GitHub's Contents API creates one commit per file. For large projects, use Git Trees/Blobs/Refs for a single atomic commit.
- Projects are stored on local disk and associated with the signed session ID. This starter uses in-memory GitHub token storage; restarting the backend invalidates GitHub connections. Use Redis or encrypted database-backed sessions/token storage for multi-worker/production deployment.
- Configure HTTPS, strong `SESSION_SECRET`, strict CORS, rate limiting, CSRF protections for state-changing endpoints, malware scanning, upload quotas, persistent database storage, audit logs, and a GitHub App before public deployment.
- AI-generated code is untrusted: review and test it before running or publishing.

## Smoke tests

```bash
curl http://localhost:8000/api/health
```

Then use the web UI to analyze a brief, generate files, preview, download, and authorize GitHub. API calls that invoke AI or GitHub correctly return configuration/authentication errors until credentials are supplied; they do not return mock data.
