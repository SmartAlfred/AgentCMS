# Embed AgentCMS into Any Website

Drop AgentCMS into an existing website — marketing site, WordPress blog, Next.js app,
Astro, static HTML — with **one script tag**, no build step, no npm, no framework.

## Quick Start

```html
<script
  src="https://your-cms-domain.com/embed/v1/agentcms.js"
  data-site-token="cap_blog_abc123..."
  data-mount="#cms"
></script>
<div id="cms"></div>
```

That's it. Your published posts now render inside `#cms`.

---

## How It Works

| Piece | Description |
|-------|-------------|
| **Embed Script** | `/embed/v1/agentcms.js` — vanilla JS, ~3 KB gzipped, no dependencies |
| **Data Endpoint** | `/embed/v1/posts?token=cap_...` — returns published posts as JSON |
| **Token** | Capability link with `posts:read` scope only (write tokens rejected) |
| **CORS** | Controlled by `EMBED_ORIGINS` env var (deny-all by default) |
| **Theming** | CSS custom properties (`--agentcms-*`) — override in your CSS |

---

## Step-by-Step Setup

### 1. Create a Capability Link for Embedding

The embed script needs a **read-only** capability token. `make seed` (i.e. `python -m scripts.seed`) prints one as
`Embed token (read-only): cap_...`; the token it prints as `Capability token:`
carries `posts:write`/`posts:publish` and is **rejected** by `/embed/v1/posts` (that
is the point -- an embed token must not be able to write).

**Today (until #44)**: neither the API nor the dashboard can mint a capability
link yet -- the documented route `POST /v1/sites/{slug}/capability-links` does not
exist, and the dashboard has no Capability Links screen. The seed script mints one
and prints it:

```bash
docker compose --env-file .env -f deploy/compose/docker-compose.prod.yml \
  exec -T api python -m scripts.seed
#   Capability token:  cap_blog_xxxxxxxx
```

### 2. Configure Allowed Origins

In your `.env` (or platform env vars), set `EMBED_ORIGINS` to your frontend origin(s):

```bash
# Single origin
EMBED_ORIGINS=https://www.example.com

# Multiple origins (comma-separated)
EMBED_ORIGINS=https://www.example.com,https://blog.example.com,https://app.example.com
```

**Default is deny-all** — the embed script will fail with a CORS error if this isn't set.

> **Behind a reverse proxy or the Caddy edge:** run `scripts/selfhost.sh` (or
> `make selfhost`) after editing `EMBED_ORIGINS`. It derives
> `EMBED_ORIGINS_REGEX` — the pipe-joined, escaped form the Caddy edge matches on
> — from your list, so the edge and the API can never disagree. A value
> hand-written into `.env` will be overwritten, and a comma-separated list is not
> a regex. Deploying with `docker compose` directly (without `selfhost.sh`) leaves
> the edge silent: it stops short-circuiting preflights and the API's own
> deny-all-or-allow answer reaches the browser unchanged. See
> [configuration.md](configuration.md#where-embed-cors-is-decided).

### 3. Add the Script Tag

Place the script tag **where you want the content to appear**, or in `<head>`:

```html
<script
  src="https://your-cms-domain.com/embed/v1/agentcms.js"
  data-site-token="cap_blog_abc123..."
  data-mount="#cms"
  data-limit="10"
  data-theme="auto"
></script>
<div id="cms"></div>
```

### Script Attributes

| Attribute | Required | Default | Description |
|-----------|----------|---------|-------------|
| `src` | Yes | — | `/embed/v1/agentcms.js` on your CMS domain |
| `data-site-token` | Yes | — | Capability token (`cap_...`) with `posts:read` scope |
| `data-mount` | No | `#cms` | CSS selector for mount point |
| `data-limit` | No | `10` | Max posts to show (1-50) |
| `data-theme` | No | `auto` | `light`, `dark`, or `auto` (follows OS) |

---

## Theming: Match Your Site's Design

The embed uses **CSS custom properties** — override them in your global CSS:

```css
:root {
  /* Colors */
  --agentcms-color-primary: #your-brand-color;
  --agentcms-color-primary-hover: #your-brand-hover;
  --agentcms-color-bg: #your-background;
  --agentcms-color-bg-alt: #your-alt-background;
  --agentcms-color-text: #your-text-color;
  --agentcms-color-text-muted: #your-muted-text;
  --agentcms-color-border: #your-border-color;
  --agentcms-color-link: #your-link-color;
  --agentcms-color-link-hover: #your-link-hover;
  --agentcms-color-error: #your-error-color;
  --agentcms-color-focus: #your-focus-ring;

  /* Typography */
  --agentcms-font-sans: your-font-stack, sans-serif;
  --agentcms-font-mono: your-mono-stack, monospace;
  --agentcms-text-xs: 0.75rem;
  --agentcms-text-sm: 0.875rem;
  --agentcms-text-base: 1rem;
  --agentcms-text-lg: 1.125rem;
  --agentcms-text-xl: 1.25rem;
  --agentcms-text-2xl: 1.5rem;

  /* Spacing */
  --agentcms-spacing-xs: 0.25rem;
  --agentcms-spacing-sm: 0.5rem;
  --agentcms-spacing-md: 1rem;
  --agentcms-spacing-lg: 1.5rem;
  --agentcms-spacing-xl: 2rem;

  /* Borders & Radius */
  --agentcms-radius-sm: 0.25rem;
  --agentcms-radius-md: 0.5rem;
  --agentcms-radius-lg: 0.75rem;
  --agentcms-radius: var(--agentcms-radius-md);

  /* Shadows */
  --agentcms-shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.05);
  --agentcms-shadow-md: 0 4px 6px -1px rgb(0 0 0 / 0.1);
  --agentcms-shadow-lg: 0 10px 15px -3px rgb(0 0 0 / 0.1);

  /* Transitions */
  --agentcms-transition-fast: 150ms ease;
  --agentcms-transition-normal: 250ms ease;
}
```

### Dark Mode

The embed **automatically respects `prefers-color-scheme`**. Override dark mode values:

```css
@media (prefers-color-scheme: dark) {
  :root {
    --agentcms-color-bg: #0f172a;
    --agentcms-color-bg-alt: #1e293b;
    --agentcms-color-text: #f1f5f9;
    --agentcms-color-text-muted: #94a3b8;
    --agentcms-color-border: #334155;
    --agentcms-color-link: #60a5fa;
    --agentcms-color-link-hover: #93c5fd;
  }
}
```

Or force a theme via `data-theme="light"` / `data-theme="dark"` on the script tag.

---

## Framework-Specific Snippets

### Plain HTML

```html
<!DOCTYPE html>
<html>
<head>
  <link rel="stylesheet" href="your-styles.css">
  <style>
    :root {
      --agentcms-color-primary: #2563eb;
      --agentcms-font-sans: system-ui, sans-serif;
    }
  </style>
</head>
<body>
  <main>
    <h1>My Site</h1>
    <section id="blog"></section>
  </main>

  <script
    src="https://cms.example.com/embed/v1/agentcms.js"
    data-site-token="cap_blog_xxx..."
    data-mount="#blog"
  ></script>
</body>
</html>
```

### WordPress

**Option A: Block Editor (Custom HTML Block)**
1. Add a **Custom HTML** block where you want the blog
2. Paste the script tag + mount div

**Option B: Theme Template (PHP)**
```php
<!-- In your theme's template file (e.g., page-blog.php) -->
<div id="cms-blog"></div>
<script
  src="https://cms.example.com/embed/v1/agentcms.js"
  data-site-token="<?php echo esc_attr(get_option('agentcms_embed_token')); ?>"
  data-mount="#cms-blog"
></script>
```

**Option C: Plugin (Code Snippets / functions.php)**
```php
function agentcms_embed_script() {
  $token = get_option('agentcms_embed_token');
  if (!$token) return;
  echo '<div id="cms-blog"></div>';
  echo '<script src="https://cms.example.com/embed/v1/agentcms.js" data-site-token="' . esc_attr($token) . '" data-mount="#cms-blog"></script>';
}
add_action('wp_footer', 'agentcms_embed_script');
```

### Next.js (App Router)

```tsx
// app/blog/page.tsx
'use client';

import { useEffect } from 'react';

export default function BlogPage() {
  useEffect(() => {
    // Dynamically inject the script (Next.js strips script tags in JSX)
    const script = document.createElement('script');
    script.src = 'https://cms.example.com/embed/v1/agentcms.js';
    script.dataset.siteToken = process.env.NEXT_PUBLIC_AGENTCMS_EMBED_TOKEN!;
    script.dataset.mount = '#cms-blog';
    script.async = true;
    document.head.appendChild(script);

    return () => script.remove();
  }, []);

  return (
    <section>
      <h1>Blog</h1>
      <div id="cms-blog" />
    </section>
  );
}
```

**With `next/script` (Next.js 13+):**
```tsx
import Script from 'next/script';

export default function BlogPage() {
  return (
    <section>
      <h1>Blog</h1>
      <div id="cms-blog" />
      <Script
        id="agentcms-embed"
        src="https://cms.example.com/embed/v1/agentcms.js"
        data-site-token={process.env.NEXT_PUBLIC_AGENTCMS_EMBED_TOKEN}
        data-mount="#cms-blog"
        strategy="lazyOnload"
      />
    </section>
  );
}
```

### Astro

```astro
---
// src/pages/blog.astro
const embedToken = import.meta.env.PUBLIC_AGENTCMS_EMBED_TOKEN;
---

<html>
  <head>
    <style>
      :root {
        --agentcms-color-primary: #your-brand;
        --agentcms-font-sans: inherit;
      }
    </style>
  </head>
  <body>
    <main>
      <h1>Blog</h1>
      <div id="cms-blog" />
    </main>

    {embedToken && (
      <script
        src="https://cms.example.com/embed/v1/agentcms.js"
        data-site-token={embedToken}
        data-mount="#cms-blog"
        is:inline
      ></script>
    )}
  </body>
</html>
```

### React (Generic)

```jsx
import { useEffect, useRef } from 'react';

function AgentCMSEmbed({ token, mountId = 'cms-blog', limit = 10 }) {
  const mounted = useRef(false);

  useEffect(() => {
    if (mounted.current) return;
    mounted.current = true;

    const script = document.createElement('script');
    script.src = 'https://cms.example.com/embed/v1/agentcms.js';
    script.dataset.siteToken = token;
    script.dataset.mount = `#${mountId}`;
    script.dataset.limit = String(limit);
    script.async = true;
    document.head.appendChild(script);

    return () => script.remove();
  }, [token, mountId, limit]);

  return <div id={mountId} />;
}

// Usage
<AgentCMSEmbed token={import.meta.env.VITE_AGENTCMS_EMBED_TOKEN} />
```

---

## CSP (Content Security Policy) Configuration

If your host site has a strict CSP, you'll need to allow:

### Script Source
```http
Content-Security-Policy: script-src 'self' https://your-cms-domain.com;
```
Or with a nonce/hash:
```http
Content-Security-Policy: script-src 'self' 'nonce-<generated>' https://your-cms-domain.com;
```

### Connect Source (for fetch to `/embed/v1/posts`)
```http
Content-Security-Policy: connect-src 'self' https://your-cms-domain.com;
```

### Frame Ancestors (if using iframe fallback)
```http
Content-Security-Policy: frame-ancestors 'self' https://your-cms-domain.com;
```

### Complete Example
```http
Content-Security-Policy:
  default-src 'self';
  script-src 'self' https://cms.example.com;
  connect-src 'self' https://cms.example.com;
  img-src 'self' https://cms.example.com data:;
  style-src 'self' 'unsafe-inline'; /* embed injects <style> */
  frame-ancestors 'self';
```

> **Note**: The embed script injects a `<style>` tag for theming. If your CSP
> blocks inline styles, either:
> 1. Add `'unsafe-inline'` to `style-src` (as above), or
> 2. Extract the CSS from the script and serve it as a static file from your domain

---

## Iframe Fallback (For Hosts Blocking Third-Party Scripts)

If your host blocks third-party scripts entirely, use the iframe fallback:

```html
<iframe
  src="https://your-cms-domain.com/embed/v1/iframe?token=cap_blog_xxx..."
  title="Blog Posts"
  style="width: 100%; min-height: 400px; border: none;"
  sandbox="allow-scripts allow-same-origin allow-links"
></iframe>
```

**Requirements:**
- Add `frame-ancestors` to your CSP (see above)
- The iframe version is served from `/embed/v1/iframe` (same token auth)
- Limited styling control (inherits from CMS, not host)

---

## Static Export Fallback (Zero-JS)

For maximum compatibility (email, AMP, strict CSP), use the static export:

```bash
# On your CMS server
docker compose exec api python -m app.api.v1.export --site blog --output /tmp/export

# Serves static HTML files at /{site}/{slug}.html
# Copy to your static host (S3, Netlify, Cloudflare Pages, etc.)
```

Or fetch JSON directly and render server-side:

```bash
# Public JSON feed (no token needed)
curl https://your-cms-domain.com/blog/posts.json

# Or token-scoped embed endpoint
curl "https://your-cms-domain.com/embed/v1/posts?token=cap_blog_xxx..."
```

---

## API Reference

### GET `/embed/v1/agentcms.js`
Returns the embed script. Cached for 1 hour.

### GET `/embed/v1/posts`
Returns published posts for the token's site.

**Query Parameters:**
| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `token` | string | required | Capability token (`cap_...`) |
| `limit` | int | 10 | Max posts (1-50) |
| `page` | int | 1 | Page number |
| `tag` | string | — | Filter by tag slug |

**Response:**
```json
{
  "site_name": "My Blog",
  "site_slug": "blog",
  "posts": [
    {
      "id": "uuid",
      "title": "Post Title",
      "slug": "post-title",
      "url": "https://cms.example.com/blog/post-title",
      "excerpt": "First paragraph...",
      "published_at": "2024-01-15T10:30:00Z",
      "site_slug": "blog",
      "tags": [
        { "name": "Tag", "slug": "tag", "url": "https://cms.example.com/blog/tags/tag" }
      ]
    }
  ]
}
```

**Errors:**
| Code | Cause |
|------|-------|
| 401 | Invalid/missing/expired/revoked token |
| 403 | Token lacks `posts:read` scope (write token used) |
| 404 | Site not found |
| 429 | Rate limited |

### GET `/embed/v1/config`
Returns embed configuration for debugging.

---

## Security Checklist

- [ ] `EMBED_ORIGINS` set to your exact frontend origin(s) — no wildcards
- [ ] Embed token has **only** `posts:read` scope — never `posts:write` or `posts:publish`
- [ ] Token TTL is reasonable (1 year max recommended)
- [ ] CSP allows `script-src` and `connect-src` for your CMS domain
- [ ] HTTPS enforced on both CMS and frontend
- [ ] Token not exposed in client-side source maps or repo

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Blank mount point | Check browser console for CORS errors; verify `EMBED_ORIGINS` |
| "Failed to load posts: 403" | Token has write scope — create a `posts:read` only link |
| "Failed to load posts: 401" | Token expired/revoked — create a new capability link |
| Styles look wrong | Override `--agentcms-*` CSS variables in your global CSS |
| Dark mode not working | Ensure your CSS doesn't override `--agentcms-color-*` without `@media` |
| Script blocked by CSP | Add `script-src https://your-cms-domain.com` to CSP |

---

## Versioning & Stability

- Embed endpoint is **versioned** at `/embed/v1/`
- Contract is covered by tests (see `tests/test_embed.py`)
- Breaking changes will go to `/embed/v2/` with advance notice
- Script URL includes version: `/embed/v1/agentcms.js`

---

## Related Docs

- [Quickstart](deploy/quickstart.md) — Deploy CMS in 5 minutes
- [Configuration](deploy/configuration.md) — All env vars
- [API Reference](../api.md) — Full OpenAPI at `/openapi.json`