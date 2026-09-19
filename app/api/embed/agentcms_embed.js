/**
 * AgentCMS Embed Script v1
 *
 * Usage:
 *   <script
 *     src="https://<host>/embed/v1/agentcms.js"
 *     data-site-token="cap_blog_xxx..."
 *     data-mount="#cms"
 *   ></script>
 *   <div id="cms"></div>
 *
 * Features:
 * - Vanilla JS, no framework, no npm, no bundler
 * - Self-contained (no external CDN dependency)
 * - Progressive: host page works if script fails
 * - Fetches published posts from /embed/v1/posts endpoint
 * - Renders with semantic HTML and CSS custom properties for theming
 * - Light/dark mode support via prefers-color-scheme
 * - Graceful error handling (never breaks host page)
 */

(function () {
  'use strict';

  // ---- Configuration from script tag ----
  const scriptTag = document.currentScript;
  if (!scriptTag) {
    console.warn('[AgentCMS Embed] No script tag found; aborting.');
    return;
  }

  const config = {
    baseUrl: scriptTag.src.replace(/\/embed\/v1\/agentcms\.js.*$/, ''),
    token: scriptTag.getAttribute('data-site-token'),
    mountSelector: scriptTag.getAttribute('data-mount') || '#cms',
    theme: scriptTag.getAttribute('data-theme') || 'auto', // 'light', 'dark', 'auto'
    limit: parseInt(scriptTag.getAttribute('data-limit') || '10', 10),
  };

  // ---- Validation ----
  if (!config.token) {
    console.warn('[AgentCMS Embed] Missing data-site-token; aborting.');
    return;
  }

  if (!config.token.startsWith('cap_')) {
    console.warn('[AgentCMS Embed] Invalid token format (expected cap_...); aborting.');
    return;
  }

  const mountPoint = document.querySelector(config.mountSelector);
  if (!mountPoint) {
    console.warn(`[AgentCMS Embed] Mount point "${config.mountSelector}" not found; aborting.`);
    return;
  }

  // ---- CSS Custom Properties (Theming) ----
  // These can be overridden by the host page via :root or a parent selector
  const defaultStyles = `
    :root {
      /* Colors - override these in your CSS to match your site */
      --agentcms-color-primary: #2563eb;
      --agentcms-color-primary-hover: #1d4ed8;
      --agentcms-color-bg: #ffffff;
      --agentcms-color-bg-alt: #f8fafc;
      --agentcms-color-text: #0f172a;
      --agentcms-color-text-muted: #64748b;
      --agentcms-color-border: #e2e8f0;
      --agentcms-color-link: #2563eb;
      --agentcms-color-link-hover: #1d4ed8;
      --agentcms-color-error: #dc2626;
      --agentcms-color-focus: #2563eb;

      /* Spacing & Layout */
      --agentcms-spacing-xs: 0.25rem;
      --agentcms-spacing-sm: 0.5rem;
      --agentcms-spacing-md: 1rem;
      --agentcms-spacing-lg: 1.5rem;
      --agentcms-spacing-xl: 2rem;

      /* Typography */
      --agentcms-font-sans: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      --agentcms-font-mono: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, monospace;
      --agentcms-text-xs: 0.75rem;
      --agentcms-text-sm: 0.875rem;
      --agentcms-text-base: 1rem;
      --agentcms-text-lg: 1.125rem;
      --agentcms-text-xl: 1.25rem;
      --agentcms-text-2xl: 1.5rem;

      /* Borders & Radius */
      --agentcms-radius-sm: 0.25rem;
      --agentcms-radius-md: 0.5rem;
      --agentcms-radius-lg: 0.75rem;
      --agentcms-radius: var(--agentcms-radius-md);

      /* Shadows */
      --agentcms-shadow-sm: 0 1px 2px 0 rgb(0 0 0 / 0.05);
      --agentcms-shadow-md: 0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1);
      --agentcms-shadow-lg: 0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1);

      /* Transitions */
      --agentcms-transition-fast: 150ms ease;
      --agentcms-transition-normal: 250ms ease;
    }

    /* Dark mode (auto via prefers-color-scheme) */
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

    /* Host can force light/dark by setting data-theme on the script tag or
       by overriding these variables in their own CSS. */

    /* ---- Component Styles ---- */
    .agentcms-embed {
      font-family: var(--agentcms-font-sans);
      color: var(--agentcms-color-text);
      background: var(--agentcms-color-bg);
      border: 1px solid var(--agentcms-color-border);
      border-radius: var(--agentcms-radius);
      padding: var(--agentcms-spacing-lg);
      max-width: 100%;
      box-shadow: var(--agentcms-shadow-md);
      line-height: 1.6;
    }

    .agentcms-embed * {
      box-sizing: border-box;
    }

    .agentcms-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: var(--agentcms-spacing-lg);
      padding-bottom: var(--agentcms-spacing-md);
      border-bottom: 1px solid var(--agentcms-color-border);
    }

    .agentcms-title {
      font-size: var(--agentcms-text-xl);
      font-weight: 600;
      color: var(--agentcms-color-text);
      margin: 0;
      text-decoration: none;
    }

    .agentcms-title:hover {
      color: var(--agentcms-color-primary);
    }

    .agentcms-posts {
      list-style: none;
      padding: 0;
      margin: 0;
    }

    .agentcms-post {
      padding: var(--agentcms-spacing-md) 0;
      border-bottom: 1px solid var(--agentcms-color-border);
    }

    .agentcms-post:last-child {
      border-bottom: none;
    }

    .agentcms-post-link {
      display: block;
      text-decoration: none;
      color: inherit;
      transition: color var(--agentcms-transition-fast);
    }

    .agentcms-post-link:hover {
      color: var(--agentcms-color-primary);
    }

    .agentcms-post-title {
      font-size: var(--agentcms-text-lg);
      font-weight: 600;
      margin: 0 0 var(--agentcms-spacing-xs);
      color: var(--agentcms-color-text);
    }

    .agentcms-post-excerpt {
      font-size: var(--agentcms-text-sm);
      color: var(--agentcms-color-text-muted);
      margin: 0 0 var(--agentcms-spacing-sm);
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .agentcms-post-meta {
      display: flex;
      flex-wrap: wrap;
      gap: var(--agentcms-spacing-sm);
      font-size: var(--agentcms-text-xs);
      color: var(--agentcms-color-text-muted);
    }

    .agentcms-post-date {
      font-variant-numeric: tabular-nums;
    }

    .agentcms-post-tags {
      display: flex;
      flex-wrap: wrap;
      gap: var(--agentcms-spacing-xs);
    }

    .agentcms-tag {
      display: inline-block;
      padding: 2px 8px;
      background: var(--agentcms-color-bg-alt);
      border: 1px solid var(--agentcms-color-border);
      border-radius: var(--agentcms-radius-sm);
      font-size: var(--agentcms-text-xs);
      color: var(--agentcms-color-text-muted);
      text-decoration: none;
    }

    .agentcms-tag:hover {
      background: var(--agentcms-color-primary);
      border-color: var(--agentcms-color-primary);
      color: white;
    }

    .agentcms-loading,
    .agentcms-error,
    .agentcms-empty {
      text-align: center;
      padding: var(--agentcms-spacing-xl);
      color: var(--agentcms-color-text-muted);
      font-size: var(--agentcms-text-sm);
    }

    .agentcms-error {
      color: var(--agentcms-color-error);
    }

    .agentcms-powered-by {
      margin-top: var(--agentcms-spacing-lg);
      padding-top: var(--agentcms-spacing-md);
      border-top: 1px solid var(--agentcms-color-border);
      text-align: right;
      font-size: var(--agentcms-text-xs);
      color: var(--agentcms-color-text-muted);
    }

    .agentcms-powered-by a {
      color: var(--agentcms-color-link);
      text-decoration: none;
    }

    .agentcms-powered-by a:hover {
      text-decoration: underline;
    }

    /* Reduced motion */
    @media (prefers-reduced-motion: reduce) {
      .agentcms-embed * {
        transition: none !important;
        animation: none !important;
      }
    }
  `;

  // ---- HTML Template ----
  function renderLoading() {
    return `
      <div class="agentcms-embed">
        <div class="agentcms-loading">Loading posts…</div>
      </div>
    `;
  }

  function renderError(message) {
    return `
      <div class="agentcms-embed">
        <div class="agentcms-error" role="alert">${escapeHtml(message)}</div>
      </div>
    `;
  }

  function renderEmpty() {
    return `
      <div class="agentcms-embed">
        <div class="agentcms-empty">No published posts yet.</div>
      </div>
    `;
  }

  function renderPosts(posts, siteName) {
    const postsHtml = posts.map(post => `
      <li class="agentcms-post">
        <a class="agentcms-post-link" href="${escapeHtml(post.url)}" target="_blank" rel="noopener noreferrer">
          <h3 class="agentcms-post-title">${escapeHtml(post.title)}</h3>
          ${post.excerpt ? `<p class="agentcms-post-excerpt">${escapeHtml(post.excerpt)}</p>` : ''}
          <div class="agentcms-post-meta">
            <time class="agentcms-post-date" datetime="${escapeHtml(post.published_at)}">${formatDate(post.published_at)}</time>
            ${post.tags && post.tags.length > 0 ? `
              <span class="agentcms-post-tags">
                ${post.tags.map(tag => `<a class="agentcms-tag" href="${escapeHtml(tag.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(tag.name)}</a>`).join('')}
              </span>
            ` : ''}
          </div>
        </a>
      </li>
    `).join('');

    return `
      <div class="agentcms-embed">
        <header class="agentcms-header">
          <a class="agentcms-title" href="${escapeHtml(config.baseUrl)}/${escapeHtml(posts[0]?.site_slug || 'blog')}" target="_blank" rel="noopener noreferrer">
            ${escapeHtml(siteName || 'Posts')}
          </a>
        </header>
        <ul class="agentcms-posts">${postsHtml}</ul>
        <div class="agentcms-powered-by">
          Powered by <a href="https://agentcms.dev" target="_blank" rel="noopener noreferrer">AgentCMS</a>
        </div>
      </div>
    `;
  }

  // ---- Utilities ----
  function escapeHtml(str) {
    if (str == null) return '';
    return String(str)
      .replace(/&/g, '&')
      .replace(/</g, '<')
      .replace(/>/g, '>')
      .replace(/"/g, '"')
      .replace(/'/g, ''');
  }

  function formatDate(isoString) {
    try {
      const date = new Date(isoString);
      return date.toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      });
    } catch {
      return isoString;
    }
  }

  function injectStyles() {
    if (document.getElementById('agentcms-embed-styles')) return;
    const style = document.createElement('style');
    style.id = 'agentcms-embed-styles';
    style.textContent = defaultStyles;
    document.head.appendChild(style);
  }

  // ---- Fetch Posts ----
  async function fetchPosts() {
    const url = `${config.baseUrl}/embed/v1/posts?token=${encodeURIComponent(config.token)}&limit=${config.limit}`;
    const response = await fetch(url, {
      method: 'GET',
      headers: {
        'Accept': 'application/json',
      },
      credentials: 'omit', // Never send cookies
    });

    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try {
        const data = await response.json();
        if (data.detail) detail = data.detail;
        if (data.hint) detail += ` — ${data.hint}`;
      } catch {
        // Ignore JSON parse errors
      }
      throw new Error(detail);
    }

    return response.json();
  }

  // ---- Main ----
  async function init() {
    injectStyles();
    mountPoint.innerHTML = renderLoading();

    try {
      const data = await fetchPosts();
      if (!data.posts || data.posts.length === 0) {
        mountPoint.innerHTML = renderEmpty();
        return;
      }
      mountPoint.innerHTML = renderPosts(data.posts, data.site_name);
    } catch (err) {
      console.error('[AgentCMS Embed] Failed to load posts:', err);
      mountPoint.innerHTML = renderError(`Failed to load posts: ${err.message}`);
    }
  }

  // Defer initialization to not block page render
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();