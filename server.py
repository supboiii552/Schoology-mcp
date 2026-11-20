import os
from fastmcp import FastMCP
from requests_oauthlib import OAuth1Session
from starlette.responses import JSONResponse

# set stateless mode in env so fastmcp constructor never rejects kwargs
os.environ["FASTMCP_STATELESS_HTTP"] = "true"

mcp = FastMCP(name="Schoology Connector")

SCHOOLOGY_KEY = os.getenv("SCHOOLOGY_KEY")
SCHOOLOGY_SECRET = os.getenv("SCHOOLOGY_SECRET")
SCHOOLOGY_DOMAIN = "https://api.schoology.com/v1"


def get_client():
  return OAuth1Session(
      client_key=SCHOOLOGY_KEY,
      client_secret=SCHOOLOGY_SECRET,
  )


# --- keep-alive health route for render free tier ---
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
  return JSONResponse({"status": "healthy", "service": "schoology-mcp"})


# --- tools exposed to gemini ---
@mcp.tool()
def get_due_and_missing_assignments() -> list[dict]:
  """Fetch all upcoming, due, and incomplete assignments across enrolled courses."""
  s = get_client()
  sections_resp = s.get(f"{SCHOOLOGY_DOMAIN}/users/me/sections")

  if sections_resp.status_code != 200:
    return [{"error": f"schoology api returned {sections_resp.status_code}"}]

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
            "course": course_name,
            "title": a.get("title"),
            "due": a.get("due"),
            "completed": a.get("completed", 0),
        })

  return assignments


@mcp.tool()
def get_course_resources(section_id: str) -> dict:
  """Retrieve documents and folders for a specific course section ID."""
  s = get_client()
  docs = s.get(f"{SCHOOLOGY_DOMAIN}/sections/{section_id}/documents").json()
  folders = s.get(f"{SCHOOLOGY_DOMAIN}/sections/{section_id}/folders").json()
  return {"documents": docs, "folders": folders}


if __name__ == "__main__":
  port = int(os.environ.get("PORT", 8080))
  mcp.run(transport="http", host="0.0.0.0", port=port)
