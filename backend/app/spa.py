"""Static file serving with a narrow single-page-app fallback.

The frontend is one index.html that switches views client-side (landing, workspace, ...), so a
browser refresh on /workspace must return index.html instead of 404. Only the listed view routes
fall back; every other unknown path still returns a real 404 (so typos and missing assets are not
masked, and API paths are never affected: API routes are registered before this mount)."""
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

VIEW_ROUTES = frozenset({"workspace", "evaluations", "documentation"})   # NOT "docs": FastAPI serves /docs


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code == 404 and path.strip("/") in VIEW_ROUTES and scope.get("method") in ("GET", "HEAD"):
                return await super().get_response("index.html", scope)
            raise
