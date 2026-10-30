import os, re, json, uuid, shutil, zipfile, io, secrets, hashlib
from pathlib import Path
from typing import Optional
import httpx
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request
from fastapi.responses import StreamingResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()
BASE = Path(os.getenv("DATA_DIR", "./data")).resolve()
PROJECTS = BASE / "projects"
PROJECTS.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="AI Project Builder API", version="1.0.0")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "CHANGE-ME-IN-PRODUCTION"), same_site="lax", https_only=os.getenv("COOKIE_SECURE", "false").lower()=="true")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:5173").split(","), allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
# OAuth tokens are held server-side, never placed in browser cookies.
github_tokens = {}

def owner(request: Request):
    if "sid" not in request.session: request.session["sid"] = secrets.token_urlsafe(24)
    return request.session["sid"]

def project_dir(pid): return PROJECTS / pid

def safe_files(pid, request):
    d = project_dir(pid)
    if not d.is_dir() or not (d / ".owner").exists() or (d / ".owner").read_text() != owner(request):
        raise HTTPException(404, "Project not found")
    result=[]
    for p in sorted(d.rglob("*")):
        if p.is_file() and p.name != ".owner":
            rel=p.relative_to(d).as_posix()
            if is_excluded(rel): continue
            result.append({"path":rel, "size":p.stat().st_size})
    return result

def is_excluded(name):
    parts=Path(name).parts
    low=name.lower()
    return (Path(name).name in {".env", ".owner"} or ".env." in Path(name).name and Path(name).name != ".env.example"
            or any(x in parts for x in ["node_modules","venv",".venv","__pycache__",".git","dist","build",".pytest_cache"])
            or low.endswith((".pyc",".pyo")) or any(k in low for k in ["id_rsa","credentials.json"]))

def extract_text(filename, data):
    ext=Path(filename).suffix.lower()
    if ext==".txt": return data.decode("utf-8", errors="replace")
    if ext==".docx":
        from docx import Document
        doc=Document(io.BytesIO(data))
        return "\n".join(p.text for p in doc.paragraphs)
    if ext==".pdf":
        import fitz
        doc=fitz.open(stream=data, filetype="pdf")
        return "\n".join(page.get_text() for page in doc)
    if ext in {".png",".jpg",".jpeg",".webp"}:
        try:
            from PIL import Image
            import pytesseract
            return pytesseract.image_to_string(Image.open(io.BytesIO(data)))
        except Exception as e:
            raise HTTPException(400, "Image OCR requires Pillow and a locally installed Tesseract binary.") from e
    raise HTTPException(400, "Supported uploads: PDF, DOCX, TXT, PNG, JPG, JPEG, WEBP.")

async def ai_json(system, prompt):
    key=os.getenv("AI_API_KEY")
    if not key: raise HTTPException(503, "AI provider is not configured. Set AI_API_KEY and AI_BASE_URL in backend/.env.")
    url=os.getenv("AI_BASE_URL","https://api.openai.com/v1").rstrip("/")+"/chat/completions"
    model=os.getenv("AI_MODEL","gpt-4o-mini")
    async with httpx.AsyncClient(timeout=180) as client:
        r=await client.post(url, headers={"Authorization":f"Bearer {key}"}, json={"model":model,"temperature":0.2,"response_format":{"type":"json_object"},"messages":[{"role":"system","content":system},{"role":"user","content":prompt}]})
    if r.status_code>=400: raise HTTPException(502, f"AI provider error ({r.status_code}): {r.text[:500]}")
    try: return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as e: raise HTTPException(502, "AI provider returned invalid JSON.") from e

class AnalyzeIn(BaseModel):
    topic: str = Field(min_length=10, max_length=20000)
    stack: str = "React + Vite + FastAPI"

class GenerateIn(AnalyzeIn):
    pass

@app.get("/api/health")
def health(): return {"status":"ok"}

@app.post("/api/projects/analyze")
async def analyze(body: AnalyzeIn, request: Request):
    data=await ai_json("You are a software architect. Return only JSON with keys summary, features (array), architecture (array), risks (array), recommended_files (array).", f"Analyze this project specification for stack {body.stack}:\n{body.topic}")
    return data

@app.post("/api/projects/generate")
async def generate(request: Request, topic: str = Form(...), stack: str = Form("React + Vite + FastAPI"), file: Optional[UploadFile] = File(None)):
    if len(topic)>20000: raise HTTPException(413,"Topic is too long.")
    spec=topic.strip()
    if file:
        if file.size and file.size>10*1024*1024: raise HTTPException(413,"Upload limit is 10 MB.")
        raw=await file.read()
        if len(raw)>10*1024*1024: raise HTTPException(413,"Upload limit is 10 MB.")
        spec += "\n\nUPLOADED SPECIFICATION:\n"+extract_text(file.filename or "upload.txt",raw)
    if len(spec)<10: raise HTTPException(400,"Enter a project topic or upload a specification.")
    prompt=f"""Create a complete, runnable project based on the specification below. Preferred stack: {stack}.
Return JSON only: {{ "project_name": "kebab-case", "files": [{{"path":"relative/path","content":"complete file text"}}] }}.
Include all required source code, sensible tests where practical, README.md, .gitignore, .env.example, dependency manifests, and configuration. Do not include real credentials, .env, binaries, node_modules, lockfiles, or generated builds. Keep each file self-contained and ensure frontend/backend APIs agree. Use secure defaults. Include working integration code but document required external credentials. Maximum 45 files; prioritize a functioning MVP. File paths must be relative, use forward slashes, and must not contain .., absolute paths, or hidden credential files.
SPECIFICATION:\n{spec}"""
    result=await ai_json("You are a senior full-stack engineer. Generate secure, executable project files. Output valid JSON only.",prompt)
    entries=result.get("files")
    if not isinstance(entries,list) or not entries: raise HTTPException(502,"AI did not return a usable file list.")
    pid=str(uuid.uuid4())
    dest=project_dir(pid); dest.mkdir(parents=True)
    (dest/".owner").write_text(owner(request))
    count=0
    for item in entries:
        rel=item.get("path","")
        content=item.get("content","")
        p=Path(rel)
        if not rel or p.is_absolute() or ".." in p.parts or len(rel)>240 or not isinstance(content,str): continue
        if is_excluded(rel): continue
        target=(dest/p).resolve()
        if dest.resolve() not in target.parents: continue
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(content,encoding="utf-8")
        count+=1
    if count==0: shutil.rmtree(dest,ignore_errors=True); raise HTTPException(502,"No safe project files were generated.")
    (dest/"project.json").write_text(json.dumps({"name":result.get("project_name","generated-project"),"stack":stack},indent=2))
    return {"project_id":pid,"project_name":result.get("project_name","generated-project"),"file_count":count,"files":safe_files(pid,request)}

@app.get("/api/projects/{project_id}/files")
def list_files(project_id: str, request: Request):
    return {"files":safe_files(project_id,request)}

@app.get("/api/projects/{project_id}/file")
def get_file(project_id: str, path: str, request: Request):
    safe_files(project_id,request)
    p=Path(path)
    if p.is_absolute() or ".." in p.parts or is_excluded(path): raise HTTPException(400,"Invalid file path.")
    target=(project_dir(project_id)/p).resolve()
    if project_dir(project_id).resolve() not in target.parents or not target.is_file(): raise HTTPException(404,"File not found.")
    return {"path":path,"content":target.read_text(encoding="utf-8",errors="replace")}

@app.get("/api/projects/{project_id}/download")
def download(project_id: str, request: Request):
    safe_files(project_id,request)
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as z:
        for f in safe_files(project_id,request):
            z.write(project_dir(project_id)/f["path"],arcname=f["path"])
    buf.seek(0)
    return StreamingResponse(buf,media_type="application/zip",headers={"Content-Disposition":f'attachment; filename="{project_id}.zip"'})

@app.get("/api/github/login")
def github_login(request: Request):
    cid=os.getenv("GITHUB_CLIENT_ID")
    if not cid: raise HTTPException(503,"Configure GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET.")
    state=secrets.token_urlsafe(32); request.session["oauth_state"]=state
    from urllib.parse import urlencode
    callback=os.getenv("GITHUB_CALLBACK_URL","http://localhost:8000/api/github/callback")
    params=urlencode({"client_id":cid,"redirect_uri":callback,"scope":"repo","state":state})
    return RedirectResponse("https://github.com/login/oauth/authorize?"+params)

@app.get("/api/github/callback")
async def github_callback(request: Request, code: str, state: str):
    if not secrets.compare_digest(str(request.session.pop("oauth_state","")),state): raise HTTPException(400,"Invalid OAuth state.")
    async with httpx.AsyncClient(timeout=30) as client:
        r=await client.post("https://github.com/login/oauth/access_token",headers={"Accept":"application/json"},json={"client_id":os.getenv("GITHUB_CLIENT_ID"),"client_secret":os.getenv("GITHUB_CLIENT_SECRET"),"code":code,"redirect_uri":os.getenv("GITHUB_CALLBACK_URL","http://localhost:8000/api/github/callback")})
    data=r.json()
    if r.status_code>=400 or "access_token" not in data: raise HTTPException(502,"GitHub OAuth exchange failed.")
    github_tokens[owner(request)]=data["access_token"]
    return RedirectResponse(os.getenv("FRONTEND_URL","http://localhost:5173")+"?github=connected")

@app.get("/api/github/status")
def github_status(request: Request): return {"connected":owner(request) in github_tokens}

async def gh(request, method, url, **kwargs):
    token=github_tokens.get(owner(request))
    if not token: raise HTTPException(401,"Connect GitHub first.")
    async with httpx.AsyncClient(timeout=60) as client:
        r=await client.request(method,url,headers={"Authorization":f"Bearer {token}","Accept":"application/vnd.github+json","X-GitHub-Api-Version":"2022-11-28"},**kwargs)
    if r.status_code==403 and r.headers.get("X-RateLimit-Remaining")=="0": raise HTTPException(429,"GitHub API rate limit reached.")
    if r.status_code>=400: raise HTTPException(r.status_code, f"GitHub API: {r.text[:600]}")
    return r.json() if r.content else {}

@app.get("/api/github/repositories")
async def repositories(request: Request):
    return await gh(request,"GET","https://api.github.com/user/repos",params={"per_page":100,"sort":"updated"})

class PublishIn(BaseModel):
    project_id: str
    mode: str = "existing"
    repo_name: str
    description: str = ""
    visibility: str = "private"
    branch: str = "main"
    confirm: bool = False

@app.post("/api/github/publish")
async def publish(body: PublishIn, request: Request):
    if not body.confirm: raise HTTPException(400,"Explicit confirmation is required.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}",body.repo_name): raise HTTPException(400,"Invalid repository name.")
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,120}",body.branch) or ".." in body.branch: raise HTTPException(400,"Invalid branch.")
    files=safe_files(body.project_id,request)
    if body.mode=="new":
        repo=await gh(request,"POST","https://api.github.com/user/repos",json={"name":body.repo_name,"description":body.description,"private":body.visibility!="public","auto_init":True})
    elif body.mode=="existing":
        me=await gh(request,"GET","https://api.github.com/user")
        repo=await gh(request,"GET",f"https://api.github.com/repos/{me['login']}/{body.repo_name}")
    else: raise HTTPException(400,"mode must be new or existing")
    owner_name=repo["owner"]["login"]; repo_name=repo["name"]
    # Ensure target branch exists; for a newly initialized repo, wait for its default branch.
    branch=body.branch
    try:
        await gh(request,"GET",f"https://api.github.com/repos/{owner_name}/{repo_name}/branches/{branch}")
    except HTTPException as e:
        if e.status_code==404 and body.mode=="new":
            branch=repo.get("default_branch") or "main"
        else: raise
    published=[]
    for f in files:
        rel=f["path"]; content=(project_dir(body.project_id)/rel).read_bytes()
        import base64
        url=f"https://api.github.com/repos/{owner_name}/{repo_name}/contents/{rel}"
        payload={"message":f"Add/update {rel} via AI Project Builder","content":base64.b64encode(content).decode(),"branch":branch}
        try:
            old=await gh(request,"GET",url,params={"ref":branch})
            payload["sha"]=old["sha"]
        except HTTPException as e:
            if e.status_code!=404: raise
        await gh(request,"PUT",url,json=payload)
        published.append(rel)
    return {"status":"published","repository_url":repo["html_url"],"branch":branch,"published_files":len(published),"files":published}
