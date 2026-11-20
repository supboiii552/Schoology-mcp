import os
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


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
  return JSONResponse({"status": "healthy", "service": "schoology-mcp"})


@mcp.tool()
def get_due_and_missing_assignments() -> list[dict]:
  """Fetch all upcoming, due, and incomplete assignments across enrolled courses."""
  s = get_client()
  uid, err = get_uid(s)
  if err:
    return [{"error": err}]

  sections_resp = s.get(f"{SCHOOLOGY_DOMAIN}/users/{uid}/sections")
  if sections_resp.status_code != 200:
    return [
        {"error": f"sections returned {sections_resp.status_code}: {sections_resp.text[:200]}"}
    ]

  sections = sections_resp.json().get("section", [])
  assignments = []

  for sec in sections:
    sec_id = sec.get("id")
    course_name = sec.get("course_title", "Unknown Course")
    assign_resp = s.get(f"{SCHOOLOGY_DOMAIN}/sections/{sec_id}/assignments")

    if assign_resp.status_code == 200:
      items = assign_resp.json().get("assignment", [])
      for a in items:
        assignments.append({
            "section_id": sec_id,
            "course": course_name,
            "title": a.get("title"),
            "due": a.get("due"),
            "completed": a.get("completed", 0),
        })

  return assignments


@mcp.tool()
def get_course_resources(section_id: str, folder_id: str = "0") -> dict:
  """Retrieve documents and folder contents (folder_id='0' for root) for a course section ID."""
  s = get_client()
  docs_resp = s.get(f"{SCHOOLOGY_DOMAIN}/sections/{section_id}/documents")
  folder_resp = s.get(f"{SCHOOLOGY_DOMAIN}/courses/{section_id}/folder/{folder_id}")

  return {
      "documents": docs_resp.json() if docs_resp.status_code == 200 else {"error": docs_resp.status_code},
      "folder": folder_resp.json() if folder_resp.status_code == 200 else {"error": folder_resp.status_code},
  }


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 8080))
  mcp.run(transport="http", host="0.0.0.0", port=port)
