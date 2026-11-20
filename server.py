import os
from datetime import datetime
from fastmcp import FastMCP
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


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
  return JSONResponse({"status": "healthy", "service": "schoology-mcp"})


@mcp.tool()
def get_due_and_missing_assignments(include_past: bool = False) -> list[dict]:
  """Fetch upcoming, due, and missing assignments across all enrolled courses."""
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

  # map assignment_id -> grade info (exception: 1=excused, 2=incomplete, 3=missing)
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
          "due": due,
          "status": status,
          "grade": g_info.get("grade"),
          "max_points": g_info.get("max_points"),
      })

  assignments.sort(key=lambda x: x["due"] or "9999-12-31")
  return assignments


@mcp.tool()
def get_course_resources(section_id: str, folder_id: str = "0") -> dict:
  """Retrieve documents and folder contents (folder_id='0' for root) for a course section ID."""
  s = get_client()
  docs = get_all_pages(
      s, f"{SCHOOLOGY_DOMAIN}/sections/{section_id}/documents?limit=200", "document"
  )
  for d in docs:
    d.pop("completion_status", None)

  folder_resp = s.get(
      f"{SCHOOLOGY_DOMAIN}/courses/{section_id}/folder/{folder_id}"
  )

  return {
      "documents": docs,
      "folder": (
          folder_resp.json()
          if folder_resp.status_code == 200
          else {"error": folder_resp.status_code}
      ),
  }


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 8080))
  mcp.run(transport="http", host="0.0.0.0", port=port)
