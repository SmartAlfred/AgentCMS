# Error Codes

Every error response is `application/problem+json` ([RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)).
Each response carries a `request_id` that appears in logs and audit events.

## Error code registry

| Code | HTTP Status | Meaning | Recovery the agent should attempt |
|------|-------------|---------|-----------------------------------|
| `validation-error` | 422 | Request body or query parameters fail Pydantic validation. | Read the `errors[]` array for each failing field, fix them all, and retry. `GET /openapi.json` has the exact schema. |
| `json-parse-error` | 400 | Request body is not valid JSON (truncated, syntax error). | Ensure the body is well-formed JSON and retry. |
| `unauthenticated` | 401 | Missing or invalid Authorization header. | Present a valid API token as `Authorization: Bearer <token>`, or use a capability link. |
| `forbidden` | 403 | Token lacks the required scope for this endpoint. | Use a token with the right scope, or request a new one with the needed scopes. |
| `endpoint-not-found` | 404 | No route matches the request path. | Check the path against `GET /openapi.json` — paths are versioned under `/v1`. |
| `method-not-allowed` | 405 | The HTTP method is not supported on this path. | Check the allowed methods for this path in `GET /openapi.json`. |
| `not-acceptable` | 406 | Accept header cannot be satisfied. | Request one of: `application/json`, `text/markdown`, `text/html`. |
| `slug-conflict` | 409 | A post with this slug already exists in the site. | Retry with the slug in `suggested_slug`, or PATCH the existing post. |
| `invalid-transition` | 409 | The requested status transition is not allowed. | Check `allowed_from` for valid source statuses, transition to an allowed status first. |
| `revision-conflict` | 409 | If-Match revision does not match the current revision. | Re-read the post, merge your change, and retry with the current revision. |
| `conflict` | 409 | Generic conflict (unknown subtype). | See `detail` for specifics; typically involves concurrent modification. |
| `not-found` | 404 | Generic resource not found (unknown subtype). | Verify the resource identifier exists using a GET request. |
| `post-not-found` | 404 | No post matches the given id or slug. | List posts with `GET /v1/sites/{site}/posts` to find the right id or slug, or POST to create one. |
| `site-not-found` | 404 | No site matches the given slug. | Use the demo site slug `blog`, or ask an administrator to create the site. |
| `payload-too-large` | 413 | Request body exceeds the size limit. | Reduce the request body size and retry. |
| `unsupported-media-type` | 415 | Content-Type header is not supported. | Use `application/json` for request bodies. |
| `rate-limited` | 429 | Too many requests; quota exceeded. | Wait for the duration in `Retry-After` header, then retry. |
| `content-required` | 422 | `body_md` field is empty or missing. | Send `body_md` with the Markdown body — it is the only required field on create. |
| `title-required` | 422 | Neither `title` nor a first-level heading in `body_md` was found. | Send `title`, or start `body_md` with a single `# Heading` line. |
| `slug-invalid` | 422 | Slug contains invalid characters. | Use lowercase letters, digits, and single hyphens only. Omit `slug` to derive it from the title. |
| `unprocessable-content` | 422 | Content cannot be processed (generic). | See `detail` for specifics; check field types match the schema. |
| `invalid-cursor` | 400 | Pagination cursor is invalid. | Pass back the opaque `next_cursor` value, or omit `cursor` to start from the newest post. |
| `invalid-query` | 400 | Query parameter is invalid. | Check query parameters against `GET /openapi.json` for valid values. |
| `bad-request` | 400 | Generic bad request (Starlette HTTP exception). | See `detail` for specifics; check the request against `GET /openapi.json`. |
| `internal-error` | 500 | Unexpected server error. | Retry once; if it persists, report the `request_id` — it maps to the server-side traceback. |
| `database-unavailable` | 503 | Database is not reachable. | Retry with a short backoff. If it persists, check `DATABASE_URL` and that Postgres is up. |
| `service-unavailable` | 503 | Service temporarily unavailable. | Retry with a short backoff. |

## Error response shape

```json
{
  "type": "https://agentcms.dev/problems/<code>",
  "title": "Human-readable title",
  "status": 422,
  "detail": "Specific explanation of what went wrong.",
  "instance": "/v1/sites/blog/posts",
  "code": "<code>",
  "request_id": "req_01HZX...",
  "hint": "Actionable instruction on how to fix the request.",
  "errors": [
    {
      "field": "title",
      "code": "value-too-long",
      "message": "Title must be <= 300 characters.",
      "type": "string_too_long"
    }
  ]
}
```

## Sensitive data policy

- `5xx` responses return only `request_id` and the generic message — no stack traces, SQL, file paths, or other actor data.
- Capability tokens (`cap_*`) are redacted in the `instance` field.
- API tokens (`acms_*`) are never logged.
