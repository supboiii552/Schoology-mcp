import io
import os
import re
import zipfile
from datetime import datetime
import xml.etree.ElementTree as ET
from docx import Document
from fastmcp import FastMCP
from pptx import Presentation
from pypdf import PdfReader
from requests_oauthlib import OAuth1Session
from starlette.responses import JSONResponse

os.environ["FASTMCP_STATELESS_HTTP"] = "true"

mcp = FastMCP(name="Schoology Connector")

SCHOOLOGY_KEY = os.getenv("SCHOOLOGY_KEY")
SCHOOLOGY_SECRET = os.getenv("SCHOOLOGY_SECRET")
SCHOOLOGY_DOMAIN = "https://api.schoology.com/v1"


def get_client():
  s = OAuth1Session(
      client_key=SCHOOLOGY_KEY,
      client_secret=SCHOOLOGY_SECRET,
  )
  s.headers.update({"Accept": "application/json"})
  return s


def get_uid(s: OAuth1Session) -> tuple[str | None, str | None]:
  resp = s.get(f"{SCHOOLOGY_DOMAIN}/app-user-info")
  if resp.status_code != 200:
    return None, f"app-user-info returned {resp.status_code}: {resp.text[:200]}"
  uid = resp.json().get("api_uid")
  if not uid:
    return None, f"api_uid missing from app-user-info: {resp.text[:200]}"
  return str(uid), None


def get_all_pages(s: OAuth1Session, url: str, key: str) -> list[dict]:
  items = []
  next_url = url
  while next_url:
    resp = s.get(next_url)
    if resp.status_code != 200:
      break
    data = resp.json()
    items.extend(data.get(key, []))
    next_url = data.get("links", {}).get("next")
  return items


def simplify_attachments(att: dict) -> list[dict]:
  if not isinstance(att, dict):
    return []
  out = []
  for f in att.get("files", {}).get("file", []):
    out.append({
        "type": "file",
        "title": f.get("title") or f.get("filename"),
        "extension": f.get("extension"),
        "download_path": f.get("converted_download_path") or f.get("download_path"),
    })
  for l in att.get("links", {}).get("link", []):
    out.append({
        "type": "link",
        "title": l.get("title"),
        "url": l.get("url"),
    })
  return out


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
  return JSONResponse({"status": "healthy", "service": "schoology-mcp"})


@mcp.tool()
def get_due_and_missing_assignments(include_past: bool = False) -> list[dict]:
  """Fetch upcoming, due, and missing assignments (with descriptions & attachments) across all enrolled courses."""
  s = get_client()
  uid, err = get_uid(s)
  if err:
    return [{"error": err}]

  sections_resp = s.get(f"{SCHOOLOGY_DOMAIN}/users/{uid}/sections")
  if sections_resp.status_code != 200:
    return [{
        "error": (
            f"sections returned {sections_resp.status_code}:"
            f" {sections_resp.text[:200]}"
        )
    }]

  grade_map = {}
  grades_resp = s.get(f"{SCHOOLOGY_DOMAIN}/users/{uid}/grades")
  if grades_resp.status_code == 200:
    for sec_grade in grades_resp.json().get("section", []):
      for period in sec_grade.get("period", []):
        for g in period.get("assignment", []):
          aid = str(g.get("assignment_id"))
          grade_map[aid] = {
              "grade": g.get("grade"),
              "max_points": g.get("max_points"),
              "exception": g.get("exception", 0),
          }

  now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
  sections = sections_resp.json().get("section", [])
  assignments = []

  for sec in sections:
    sec_id = sec.get("id")
    course_name = sec.get("course_title", "Unknown Course").strip()
    items = get_all_pages(
        s, f"{SCHOOLOGY_DOMAIN}/sections/{sec_id}/assignments?limit=200", "assignment"
    )

    for a in items:
      aid = str(a.get("id"))
      due = a.get("due", "")
      g_info = grade_map.get(aid, {})
      exc = g_info.get("exception", 0)
      is_missing = exc in (2, 3)
      is_upcoming = bool(due and due >= now_str)

      if not include_past and not (is_upcoming or is_missing):
        continue

      status = (
          "missing"
          if exc == 3
          else "incomplete"
          if exc == 2
          else "excused"
          if exc == 1
          else "graded"
          if g_info.get("grade") is not None
          else "upcoming/ungraded"
      )

      assignments.append({
          "section_id": sec_id,
          "course": course_name,
          "title": a.get("title"),
          "description": re.sub(r"<[^>]+>", " ", a.get("description") or "").strip(),
          "due": due,
          "status": status,
          "grade": g_info.get("grade"),
          "max_points": g_info.get("max_points"),
          "attachments": simplify_attachments(a.get("attachments", {})),
      })

  assignments.sort(key=lambda x: x["due"] or "9999-12-31")
  return assignments


@mcp.tool()
def get_course_resources(section_id: str, folder_id: str = "0") -> dict:
  """Retrieve documents, links, and folder contents (folder_id='0' for root) for a course section ID."""
  s = get_client()
  docs = get_all_pages(
      s, f"{SCHOOLOGY_DOMAIN}/sections/{section_id}/documents?limit=200", "document"
  )
  clean_docs = []
  for d in docs:
    clean_docs.append({
        "id": d.get("id"),
        "title": d.get("title"),
        "folder_id": d.get("course_fid"),
        "attachments": simplify_attachments(d.get("attachments", {})),
    })

  folder_resp = s.get(
      f"{SCHOOLOGY_DOMAIN}/courses/{section_id}/folder/{folder_id}"
  )

  return {
      "documents": clean_docs,
      "folder": (
          folder_resp.json()
          if folder_resp.status_code == 200
          else {"error": folder_resp.status_code}
      ),
  }


@mcp.tool()
def read_attachment(download_path: str) -> str:
  """Download and extract text from any Schoology attachment (PDF, DOCX, PPTX, XLSX, TXT, HTML)."""
  if not download_path.startswith("https://api.schoology.com/"):
    return "error: invalid schoology attachment url"

  s = get_client()
  resp = s.get(download_path, allow_redirects=True)
  if resp.status_code != 200:
    return f"error: attachment returned {resp.status_code}"

  data = resp.content
  path_lower = download_path.lower()
  ctype = resp.headers.get("Content-Type", "").lower()

  try:
    if "pdf" in ctype or path_lower.endswith(".pdf"):
      reader = PdfReader(io.BytesIO(data))
      text = "\n\n".join(p.extract_text() or "" for p in reader.pages).strip()
      return text[:30000] if text else "error: pdf had no extractable text (scanned image)"

    if "wordprocessingml" in ctype or path_lower.endswith(".docx"):
      doc = Document(io.BytesIO(data))
      parts = [p.text for p in doc.paragraphs if p.text.strip()]
      for table in doc.tables:
        for row in table.rows:
          parts.append(" | ".join(c.text.strip() for c in row.cells))
      return "\n".join(parts).strip()[:30000]

    if "presentationml" in ctype or path_lower.endswith(".pptx"):
      prs = Presentation(io.BytesIO(data))
      slides = []
      for i, slide in enumerate(prs.slides, 1):
        lines = [sh.text.strip() for sh in slide.shapes if sh.has_text_frame and sh.text.strip()]
        if lines:
          slides.append(f"[Slide {i}]\n" + "\n".join(lines))
      return "\n\n".join(slides).strip()[:30000]

    if "spreadsheetml" in ctype or path_lower.endswith(".xlsx"):
      with zipfile.ZipFile(io.BytesIO(data)) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
          root = ET.fromstring(z.read("xl/sharedStrings.xml"))
          strings = ["".join(t.itertext()) for t in root]
        return "\n".join(strings).strip()[:30000]

    if "html" in ctype or path_lower.endswith((".html", ".htm")):
      clean = re.sub(r"<[^>]+>", " ", resp.text)
      return re.sub(r"\s+", " ", clean).strip()[:30000]

    return resp.text[:30000]
  except Exception as e:
    return f"error parsing file: {e}"


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 8080))
  mcp.run(transport="http", host="0.0.0.0", port=port)
