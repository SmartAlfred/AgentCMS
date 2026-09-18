"""Nocturne dashboard HTML templates.

Server-rendered HTML using Nocturne CSS tokens + htmx for dynamic behaviour.
No client-side framework -- just htmx attributes on HTML elements.
"""

from __future__ import annotations

import html
from typing import Any


def _esc(text: str) -> str:
    """Escape HTML special characters."""
    return html.escape(str(text), quote=True)


# ---------------------------------------------------------------------------
# SVG icons (kept short for line-length compliance)
# ---------------------------------------------------------------------------

_ICON_HOME = '<svg viewBox="0 0 24 24"><path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>'
_ICON_DOC = '<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>'
_ICON_CHECK = '<svg viewBox="0 0 24 24"><path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"/><path d="M9 12l2 2 4-4"/></svg>'
_ICON_LOCK = '<svg viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>'
_ICON_ACTIVITY = '<svg viewBox="0 0 24 24"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>'
_ICON_SETTINGS = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z"/></svg>'
_ICON_LOGOUT = '<svg viewBox="0 0 24 24" style="width:18px;height:18px"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><polyline points="16 17 21 12 16 7" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><line x1="21" y1="12" x2="9" y2="12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>'


# ---------------------------------------------------------------------------
# Nocturne CSS (inline, ~4 KB)
# ---------------------------------------------------------------------------

NOCTURNE_CSS = """
:root{color-scheme:dark;--n-0:#FFF;--n-50:#F4F6FA;--n-100:#E6E9F2;--n-200:#CDD3E0;--n-300:#A8AFC0;--n-400:#7E8798;--n-500:#5C6474;--n-600:#414856;--n-700:#2B303C;--n-800:#1B1E2A;--n-850:#14161F;--n-900:#0E1017;--n-950:#08090D;--n-1000:#05060A;--brand-300:#A794FF;--brand-400:#8F75FF;--brand-500:#7C5CFF;--brand-600:#6A48F0;--brand-800:#3D2596;--accent-cyan:#22D3EE;--accent-magenta:#F472B6;--accent-lime:#A3E635;--success:#34D399;--warning:#FBBF24;--danger:#FB7185;--info:#38BDF8;--bg-sunken:var(--n-1000);--bg-base:var(--n-950);--bg-raised-1:var(--n-900);--bg-raised-2:var(--n-850);--bg-raised-3:var(--n-800);--bg-raised-4:var(--n-700);--bg-glass:rgba(20,22,31,.72);--bg-overlay:rgba(5,6,10,.72);--text-primary:var(--n-50);--text-secondary:var(--n-300);--text-tertiary:var(--n-400);--text-disabled:var(--n-500);--text-brand:var(--brand-300);--text-on-brand:#0B0714;--border-subtle:rgba(255,255,255,.06);--border-default:rgba(255,255,255,.10);--border-strong:rgba(255,255,255,.16);--border-brand:rgba(124,92,255,.55);--border-highlight:rgba(255,255,255,.08);--presence-online:#34D399;--presence-away:#FBBF24;--presence-busy:#FB7185;--presence-offline:var(--n-500);--gradient-aurora:linear-gradient(135deg,#7C5CFF 0%,#22D3EE 100%);--gradient-brand:linear-gradient(135deg,#8F75FF 0%,#6A48F0 100%);--gradient-brand-deep:linear-gradient(160deg,#6A48F0 0%,#3D2596 100%);--gradient-own-bubble:linear-gradient(160deg,#7C5CFF 0%,#6244E8 100%);--gradient-text:linear-gradient(92deg,#FFF 0%,#A8AFC0 100%);--gradient-text-brand:linear-gradient(92deg,#A794FF 0%,#22D3EE 100%);--gradient-glow-top:radial-gradient(120% 80% at 50% -20%,rgba(124,92,255,.28) 0%,rgba(124,92,255,0) 60%);--gradient-mesh:radial-gradient(50% 50% at 20% 20%,rgba(124,92,255,.20) 0%,transparent 70%),radial-gradient(50% 50% at 80% 30%,rgba(34,211,238,.14) 0%,transparent 70%),radial-gradient(60% 60% at 50% 90%,rgba(244,114,182,.12) 0%,transparent 70%);--font-sans:Inter,'Inter var',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;--font-display:'Inter Tight',Inter,sans-serif;--font-mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;--text-micro:11px/16px var(--font-sans);--text-caption:12px/16px var(--font-sans);--text-body-sm:13px/20px var(--font-sans);--text-body:15px/24px var(--font-sans);--text-title-md:17px/24px var(--font-sans);--text-title-lg:20px/28px var(--font-display);--text-display-sm:24px/32px var(--font-display);--tracking-tight:-.02em;--tracking-snug:-.011em;--tracking-caps:.06em;--s-1:4px;--s-2:8px;--s-3:12px;--s-4:16px;--s-5:20px;--s-6:24px;--s-8:32px;--s-10:40px;--s-12:48px;--r-xs:6px;--r-sm:10px;--r-md:14px;--r-lg:18px;--r-xl:24px;--r-full:9999px;--shadow-sm:0 2px 8px rgba(0,0,0,.36);--shadow-md:0 8px 24px rgba(0,0,0,.44);--shadow-lg:0 16px 40px rgba(0,0,0,.52);--shadow-xl:0 32px 72px rgba(0,0,0,.60);--shadow-glow-brand:0 8px 28px rgba(124,92,255,.36);--shadow-ring:inset 0 0 0 1px rgba(255,255,255,.08);--shadow-inner-top:inset 0 1px 0 rgba(255,255,255,.08);--shadow-focus:0 0 0 2px var(--bg-base),0 0 0 4px rgba(124,92,255,.75);--dur-instant:80ms;--dur-fast:140ms;--dur-base:200ms;--dur-slow:320ms;--ease-standard:cubic-bezier(.2,0,0,1);--ease-enter:cubic-bezier(0,0,.2,1);--ease-exit:cubic-bezier(.4,0,1,1);--ease-spring:cubic-bezier(.34,1.56,.64,1)}
*,*::before,*::after{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--bg-base);color:var(--text-primary);font:var(--text-body);-webkit-font-smoothing:antialiased;line-height:1.65}
::selection{background:rgba(124,92,255,.35)}
:focus-visible{outline:none;box-shadow:var(--shadow-focus);border-radius:var(--r-sm)}
a{color:var(--text-brand);text-decoration:none}
a:hover{text-decoration:underline}
*::-webkit-scrollbar{width:10px;height:10px}
*::-webkit-scrollbar-track{background:transparent}
*::-webkit-scrollbar-thumb{background:rgba(255,255,255,.14);border-radius:var(--r-full);border:2px solid transparent;background-clip:content-box}
*::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.22);background-clip:content-box}
.tnum{font-variant-numeric:tabular-nums}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important;scroll-behavior:auto!important}}
@media(max-width:360px){body{font-size:14px}}
.dash-shell{display:flex;min-height:100vh}
.dash-sidebar{width:240px;background:var(--bg-raised-1);border-right:1px solid var(--border-subtle);padding:var(--s-5) 0;flex-shrink:0;display:flex;flex-direction:column}
.dash-sidebar__logo{padding:0 var(--s-5) var(--s-5);border-bottom:1px solid var(--border-subtle);margin-bottom:var(--s-4)}
.dash-sidebar__logo h1{font:var(--text-title-md);margin:0;color:var(--text-primary)}
.dash-sidebar__logo p{font:var(--text-caption);color:var(--text-tertiary);margin:var(--s-1) 0 0}
.dash-nav{list-style:none;padding:0;margin:0;flex:1}
.dash-nav li{margin:0}
.dash-nav a,.dash-nav button{display:flex;align-items:center;gap:var(--s-3);padding:var(--s-3) var(--s-5);width:100%;text-align:left;font:var(--text-body-sm);color:var(--text-secondary);background:transparent;border:none;cursor:pointer;text-decoration:none;border-left:3px solid transparent}
.dash-nav a:hover,.dash-nav button:hover{background:var(--bg-raised-2);color:var(--text-primary);text-decoration:none}
.dash-nav a.active,.dash-nav button.active{background:var(--bg-raised-2);color:var(--text-primary);border-left-color:var(--brand-500)}
.dash-nav svg{width:18px;height:18px;flex-shrink:0;stroke:currentColor;fill:none;stroke-width:1.5;stroke-linecap:round;stroke-linejoin:round}
.dash-main{flex:1;overflow-y:auto;padding:var(--s-6);min-width:0}
.dash-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:var(--s-6);flex-wrap:wrap;gap:var(--s-4)}
.dash-header h2{font:var(--text-title-lg);margin:0}
.card{background:var(--bg-raised-2);border:1px solid var(--border-subtle);border-radius:var(--r-lg);box-shadow:var(--shadow-md),var(--shadow-inner-top);padding:var(--s-5)}
.card--raised{background:var(--bg-raised-3)}
.card-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:var(--s-4)}
.card-stat{text-align:center}
.card-stat__value{font:var(--text-display-sm);font-weight:700;color:var(--text-primary)}
.card-stat__label{font:var(--text-caption);color:var(--text-tertiary);margin-top:var(--s-1)}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:var(--s-2);height:38px;padding:0 var(--s-4);font:var(--text-body-sm);font-weight:600;color:var(--text-primary);background:var(--bg-raised-3);border:1px solid var(--border-default);border-radius:var(--r-sm);cursor:pointer;transition:all var(--dur-fast) var(--ease-standard);min-width:32px;min-height:32px}
.btn:hover{background:var(--bg-raised-4);transform:translateY(-1px);box-shadow:var(--shadow-md)}
.btn:active{transform:scale(.98);transition-duration:var(--dur-instant)}
.btn--primary{background:var(--gradient-brand);border-color:transparent;color:var(--text-on-brand);box-shadow:var(--shadow-glow-brand),var(--shadow-inner-top)}
.btn--primary:hover{filter:brightness(1.08)}
.btn--ghost{background:transparent;border-color:transparent;color:var(--text-secondary)}
.btn--ghost:hover{background:var(--bg-raised-2);box-shadow:none}
.btn--danger{background:var(--danger);border-color:transparent;color:#2A0710}
.btn--danger:hover{filter:brightness(1.1)}
.btn--sm{height:32px;padding:0 var(--s-3);font:var(--text-caption)}
.field{display:flex;flex-direction:column;gap:var(--s-2)}
.field label{font:var(--text-caption);font-weight:500;color:var(--text-secondary)}
.field input,.field select,.field textarea{height:38px;padding:0 var(--s-3);background:var(--bg-raised-2);border:1px solid var(--border-default);border-radius:var(--r-sm);color:var(--text-primary);font:var(--text-body-sm)}
.field input:focus,.field select:focus,.field textarea:focus{border-color:var(--brand-500);box-shadow:var(--shadow-focus);outline:none}
.field textarea{height:auto;min-height:80px;resize:vertical;padding:var(--s-3);font-family:var(--font-mono);font-size:13px;line-height:1.5}
.field .help{font:var(--text-micro);color:var(--text-tertiary)}
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font:var(--text-body-sm)}
th{text-align:left;padding:var(--s-3) var(--s-4);font-weight:600;color:var(--text-secondary);border-bottom:1px solid var(--border-default);white-space:nowrap}
td{padding:var(--s-3) var(--s-4);border-bottom:1px solid var(--border-subtle);vertical-align:middle}
tr:hover td{background:var(--bg-raised-2)}
.badge{display:inline-flex;align-items:center;gap:var(--s-1);padding:2px 8px;border-radius:var(--r-full);font:var(--text-micro);font-weight:600;white-space:nowrap}
.badge--draft{background:rgba(92,100,116,.2);color:var(--n-400)}
.badge--published{background:rgba(52,211,153,.15);color:var(--success)}
.badge--pending_review{background:rgba(251,191,36,.15);color:var(--warning)}
.badge--trashed{background:rgba(251,113,133,.15);color:var(--danger)}
.badge--approved{background:rgba(52,211,153,.15);color:var(--success)}
.badge--rejected{background:rgba(251,113,133,.15);color:var(--danger)}
.toast-container{position:fixed;top:var(--s-5);right:var(--s-5);z-index:9999;display:flex;flex-direction:column;gap:var(--s-2)}
.toast{background:var(--bg-raised-3);border:1px solid var(--border-default);border-radius:var(--r-md);padding:var(--s-3) var(--s-4);box-shadow:var(--shadow-lg);display:flex;align-items:center;gap:var(--s-3);max-width:360px;font:var(--text-body-sm);animation:toastIn var(--dur-base) var(--ease-enter) both}
.toast--success{border-left:3px solid var(--success)}
.toast--error{border-left:3px solid var(--danger)}
.toast--info{border-left:3px solid var(--info)}
.toast--warning{border-left:3px solid var(--warning)}
@keyframes toastIn{from{opacity:0;transform:translateY(-12px)}to{opacity:1;transform:none}}
.empty-state{text-align:center;padding:var(--s-10) var(--s-6);position:relative}
.empty-state__mesh{position:absolute;inset:0;margin:auto;width:320px;height:320px;background:var(--gradient-mesh);filter:blur(80px);opacity:.5;pointer-events:none;z-index:0}
.empty-state__content{position:relative;z-index:1}
.empty-state h3{font:var(--text-title-lg);margin:0 0 var(--s-2)}
.empty-state p{font:var(--text-body-sm);color:var(--text-secondary);max-width:42ch;margin:0 auto var(--s-5)}
.diff{font-family:var(--font-mono);font-size:13px;line-height:1.5;overflow-x:auto;padding:var(--s-4);background:var(--bg-raised-1);border-radius:var(--r-md);border:1px solid var(--border-subtle)}
.diff .added{background:rgba(52,211,153,.12);color:var(--success)}
.diff .removed{background:rgba(251,113,133,.12);color:var(--danger)}
.toggle{position:relative;width:44px;height:24px;cursor:pointer;display:inline-block}
.toggle input{opacity:0;width:0;height:0;position:absolute}
.toggle__slider{position:absolute;inset:0;background:var(--bg-raised-4);border-radius:var(--r-full);transition:background var(--dur-fast) var(--ease-standard)}
.toggle__slider::before{content:"";position:absolute;left:2px;top:2px;width:20px;height:20px;background:var(--text-primary);border-radius:var(--r-full);transition:transform var(--dur-fast) var(--ease-standard)}
.toggle input:checked+.toggle__slider{background:var(--brand-500)}
.toggle input:checked+.toggle__slider::before{transform:translateX(20px)}
.toggle input:focus-visible+.toggle__slider{box-shadow:var(--shadow-focus)}
.progress{height:8px;background:var(--bg-raised-4);border-radius:var(--r-full);overflow:hidden}
.progress__bar{height:100%;background:var(--gradient-brand);border-radius:var(--r-full);transition:width var(--dur-slow) var(--ease-standard)}
@media(max-width:768px){.dash-sidebar{display:none}.dash-main{padding:var(--s-4)}}
@media(max-width:360px){.card-grid{grid-template-columns:1fr}.dash-header{flex-direction:column;align-items:flex-start}}
"""


# ---------------------------------------------------------------------------
# Helpers for htmx CSRF headers (avoids backslash in f-strings)
# ---------------------------------------------------------------------------


def _hx_headers_csrf(csrf_token: str) -> str:
    """Return an hx-headers attribute value with the CSRF token."""
    return 'hx-headers=\'{"X-CSRF-Token":"' + _esc(csrf_token) + "\"}'"


def _hx_post_btn(url: str, label: str, csrf_token: str, cls: str = "btn btn--sm btn--ghost") -> str:
    """Build a button element with hx-post and CSRF header."""
    return (
        '<button class="'
        + cls
        + '" hx-post="'
        + url
        + '" '
        + _hx_headers_csrf(csrf_token)
        + ' hx-swap="none">'
        + label
        + "</button>"
    )


# ---------------------------------------------------------------------------
# Base page wrapper
# ---------------------------------------------------------------------------


def render_base(
    *,
    title: str,
    csrf_token: str,
    active_nav: str = "",
    body_html: str = "",
    request: Any = None,
) -> str:
    """Render the full dashboard HTML page."""
    nav_items = [
        ("/dashboard/", "overview", "Overview", _ICON_HOME),
        ("/dashboard/posts", "posts", "Posts", _ICON_DOC),
        ("/dashboard/reviews", "reviews", "Reviews", _ICON_CHECK),
        ("/dashboard/tokens", "tokens", "Tokens", _ICON_LOCK),
        ("/dashboard/activity", "activity", "Activity", _ICON_ACTIVITY),
        ("/dashboard/settings", "settings", "Settings", _ICON_SETTINGS),
    ]

    nav_html = ""
    for href, key, label, icon in nav_items:
        active_cls = " active" if key == active_nav else ""
        aria = ' aria-current="page"' if key == active_nav else ""
        nav_html += (
            '<li><a href="'
            + href
            + '" class="'
            + active_cls
            + '"'
            + aria
            + ">"
            + icon
            + "<span>"
            + label
            + "</span></a></li>\n"
        )

    logout_btn = (
        '<button type="submit" class="btn btn--ghost btn--sm" '
        'style="width:100%;justify-content:flex-start">' + _ICON_LOGOUT + "<span>Logout</span></button>"
    )

    csrf_val = _esc(csrf_token)
    toast_js = (
        'document.body.addEventListener("showToast",function(e){'
        'var t=document.createElement("div");'
        't.className="toast toast--"+e.detail.level;'
        "t.textContent=e.detail.message;"
        'document.getElementById("toast-container").appendChild(t);'
        "setTimeout(function(){t.remove()},4000)});"
    )
    hx_js = (
        'document.body.addEventListener("htmx:afterRequest",function(e){'
        'if(e.detail.xhr&&e.detail.xhr.getResponseHeader("X-Toast")){'
        'var r=JSON.parse(e.detail.xhr.getResponseHeader("X-Toast"));'
        'document.body.dispatchEvent(new CustomEvent("showToast",{detail:r}))}});'
    )
    kb_js = (
        'document.addEventListener("keydown",function(e){'
        'if(e.target.tagName==="INPUT"||e.target.tagName==="TEXTAREA")return;'
        'if(e.key==="g"){setTimeout(function(){'
        'document.addEventListener("keydown",function h(k){'
        'document.removeEventListener("keydown",h);'
        'if(k.key==="p")window.location="/dashboard/posts";'
        'if(k.key==="a")window.location="/dashboard/activity";'
        "},100)},0)}});"
    )

    return (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>" + _esc(title) + " - AgentCMS</title>"
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700'
        "&family=Inter+Tight:wght@600"
        '&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">'
        '<script src="https://unpkg.com/htmx.org@2.0.4"></script>'
        "<style>" + NOCTURNE_CSS + "</style>"
        "</head><body>"
        '<div class="dash-shell">'
        '<nav class="dash-sidebar" aria-label="Dashboard navigation">'
        '<div class="dash-sidebar__logo"><h1>AgentCMS</h1><p>Dashboard</p></div>'
        '<ul class="dash-nav">' + nav_html + "</ul>"
        '<div style="padding:var(--s-4) var(--s-5);border-top:1px solid var(--border-subtle)">'
        '<form method="post" action="/dashboard/logout">'
        '<input type="hidden" name="csrf_token" value="'
        + csrf_val
        + '">'
        + logout_btn
        + "</form></div></nav>"
        '<main class="dash-main" id="dash-content">' + body_html + "</main>"
        "</div>"
        '<div class="toast-container" id="toast-container"></div>'
        "<script>" + toast_js + hx_js + kb_js + "</script>"
        "</body></html>"
    )


# ---------------------------------------------------------------------------
# Page templates
# ---------------------------------------------------------------------------


def render_login_page(*, request: Any = None, error: str = "") -> str:
    """Render the login page."""
    error_html = ""
    if error:
        error_html = (
            '<div class="toast toast--error" '
            'style="margin-bottom:var(--s-4);display:block;position:static">' + _esc(error) + "</div>"
        )

    return (
        '<!DOCTYPE html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Login - AgentCMS</title>"
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700'
        "&family=Inter+Tight:wght@600"
        '&display=swap" rel="stylesheet">'
        "<style>"
        + NOCTURNE_CSS
        + ".login-wrap{display:grid;place-items:center;min-height:100vh;padding:var(--s-5)}"
        ".login-card{background:var(--bg-raised-2);border:1px solid var(--border-subtle);"
        "border-radius:var(--r-xl);box-shadow:var(--shadow-xl);padding:var(--s-8);"
        "width:100%;max-width:400px;text-align:center;position:relative;overflow:hidden}"
        ".login-card::before{content:'';position:absolute;top:-50%;left:-50%;"
        "width:200%;height:200%;background:var(--gradient-mesh);filter:blur(80px);"
        "opacity:.3;pointer-events:none;z-index:0}"
        ".login-card>*{position:relative;z-index:1}"
        ".login-card h1{font:var(--text-display-sm);margin:0 0 var(--s-2)}"
        ".login-card p{font:var(--text-body-sm);color:var(--text-secondary);margin:0 0 var(--s-6)}"
        ".login-card form{display:flex;flex-direction:column;gap:var(--s-4);text-align:left}"
        "</style></head><body style='background:var(--bg-base)'>"
        '<div class="login-wrap"><div class="login-card">'
        "<h1>AgentCMS</h1><p>Sign in to the dashboard</p>"
        + error_html
        + '<form method="post" action="/dashboard/magic">'
        '<div class="field"><label for="email">Email address</label>'
        '<input type="email" id="email" name="email" placeholder="you@example.com" required autofocus>'
        '<span class="help">We will log you in directly in dev mode</span></div>'
        '<button type="submit" class="btn btn--primary" '
        'style="width:100%;margin-top:var(--s-2)">Send magic link</button>'
        "</form></div></div></body></html>"
    )


def render_overview(
    *,
    site_slug: str,
    post_counts: dict[str, int],
    recent_activity: list[dict[str, Any]],
    capability_links: list[dict[str, Any]],
    kill_switch_state: dict[str, Any],
    csrf_token: str,
    request: Any = None,
) -> str:
    """Render the overview dashboard page."""
    total = sum(post_counts.values())
    status_items = [
        ("draft", post_counts.get("draft", 0)),
        ("published", post_counts.get("published", 0)),
        ("pending_review", post_counts.get("pending_review", 0)),
        ("trashed", post_counts.get("trashed", 0)),
    ]
    stats_parts: list[str] = []
    for status, count in status_items:
        pct = (count / total * 100) if total > 0 else 0
        stats_parts.append(
            '<div class="card card-stat">'
            '<div class="card-stat__value tnum">' + str(count) + "</div>"
            '<div class="card-stat__label"><span class="badge badge--'
            + status
            + '">'
            + status
            + "</span></div>"
            '<div class="progress" style="margin-top:var(--s-3)">'
            '<div class="progress__bar" style="width:' + str(int(pct)) + '%"></div>'
            "</div></div>\n"
        )
    stats_html = "".join(stats_parts)

    act_parts: list[str] = []
    for item in recent_activity[:10]:
        act_parts.append(
            "<tr><td>" + _esc(item.get("action", "")) + "</td>"
            "<td>" + _esc(item.get("actor_label", "Unknown")) + "</td>"
            "<td>"
            + _esc(item.get("target_type", ""))
            + " "
            + _esc(str(item.get("target_id", ""))[:8])
            + "</td>"
            '<td class="tnum" style="color:var(--text-tertiary)">'
            + _esc(str(item.get("created_at", ""))[:19])
            + "</td></tr>\n"
        )
    activity_html = (
        "".join(act_parts)
        if act_parts
        else (
            '<tr><td colspan="4" style="text-align:center;color:var(--text-tertiary);padding:var(--s-6)">'
            "No activity yet</td></tr>"
        )
    )

    link_parts: list[str] = []
    for link in capability_links[:5]:
        badge_cls = "published" if not link.get("revoked_at") else "trashed"
        badge_txt = "active" if not link.get("revoked_at") else "revoked"
        link_parts.append(
            "<tr><td>" + _esc(link.get("label", "")) + "</td>"
            '<td><span class="badge badge--' + badge_cls + '">' + badge_txt + "</span></td>"
            '<td class="tnum">' + str(link.get("uses_count", 0)) + "</td></tr>\n"
        )
    links_html = (
        "".join(link_parts)
        if link_parts
        else (
            '<tr><td colspan="3" style="text-align:center;color:var(--text-tertiary);padding:var(--s-6)">'
            "No links yet</td></tr>"
        )
    )

    paused = kill_switch_state.get("global", False)
    paused_cls = " checked" if paused else ""
    paused_label = "Agent writes are PAUSED" if paused else "Agent writes active"
    paused_color = "var(--danger)" if paused else "var(--success)"
    toggle_hx = (
        'hx-post="/dashboard/kill-switch/global" '
        'hx-vals=\'{"action":"toggle"}\' '
        'hx-swap="none" ' + _hx_headers_csrf(csrf_token)
    )

    body = (
        '<div class="dash-header"><h2>Overview</h2>'
        '<span class="badge badge--draft" style="font-size:12px">' + _esc(site_slug) + "</span></div>"
        '<div style="display:flex;align-items:center;gap:var(--s-4);margin-bottom:var(--s-6);'
        "padding:var(--s-4) var(--s-5);background:var(--bg-raised-2);"
        'border:1px solid var(--border-subtle);border-radius:var(--r-lg)">'
        '<label class="toggle" title="Pause all agent writes">'
        '<input type="checkbox"' + paused_cls + " " + toggle_hx + ">"
        '<span class="toggle__slider"></span></label>'
        '<span style="font:var(--text-body-sm);font-weight:600;color:'
        + paused_color
        + '">'
        + paused_label
        + "</span></div>"
        '<div class="card-grid" style="margin-bottom:var(--s-6)">' + stats_html + "</div>"
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:var(--s-4);margin-bottom:var(--s-6)">'
        '<div class="card"><h3 style="font:var(--text-title-md);margin:0 0 var(--s-4)">Recent activity</h3>'
        '<div class="table-wrap"><table>'
        "<thead><tr><th>Action</th><th>Actor</th><th>Target</th><th>When</th></tr></thead>"
        "<tbody>" + activity_html + "</tbody></table></div></div>"
        '<div class="card"><h3 style="font:var(--text-title-md);margin:0 0 var(--s-4)">Capability links</h3>'
        '<div class="table-wrap"><table>'
        "<thead><tr><th>Label</th><th>Status</th><th>Uses</th></tr></thead>"
        "<tbody>" + links_html + "</tbody></table></div></div></div>"
    )
    return render_base(
        title="Overview", csrf_token=csrf_token, active_nav="overview", body_html=body, request=request
    )


def render_posts_list(
    *,
    posts: list[dict[str, Any]],
    status_filter: str | None = None,
    next_cursor: str | None = None,
    prev_cursor: str | None = None,
    csrf_token: str,
    site_slug: str = "blog",
    request: Any = None,
) -> str:
    """Render the posts list page."""
    row_parts: list[str] = []
    for p in posts:
        status = p.get("status", "draft")
        slug = _esc(p.get("slug", p.get("id", "")))
        title = _esc(p.get("title", "Untitled"))
        actions: list[str] = []
        if status == "draft":
            url = "/dashboard/posts/" + _esc(p.get("id", "")) + "/publish"
            actions.append(_hx_post_btn(url, "Publish", csrf_token))
        if status == "published":
            url = "/dashboard/posts/" + _esc(p.get("id", "")) + "/unpublish"
            actions.append(_hx_post_btn(url, "Unpublish", csrf_token))
        del_btn = (
            '<button class="btn btn--sm btn--ghost" '
            'hx-delete="/dashboard/posts/'
            + _esc(p.get("id", ""))
            + '" '
            + _hx_headers_csrf(csrf_token)
            + ' hx-swap="none" hx-confirm="Trash this post?">Trash</button>'
        )
        actions.append(del_btn)
        row_parts.append(
            '<tr><td><a href="/dashboard/posts/' + slug + '">' + title + "</a></td>"
            '<td><span class="badge badge--' + _esc(status) + '">' + _esc(status) + "</span></td>"
            '<td class="tnum" style="color:var(--text-tertiary)">'
            + _esc(str(p.get("updated_at", ""))[:10])
            + "</td>"
            '<td style="white-space:nowrap">' + "".join(actions) + "</td></tr>\n"
        )
    rows = "".join(row_parts)

    filter_parts: list[str] = []
    for s in ["", "draft", "published", "pending_review", "trashed"]:
        label = s or "all"
        active_cls = " btn--primary" if s == (status_filter or "") else " btn--ghost"
        href = "/dashboard/posts?status=" + s if s else "/dashboard/posts"
        filter_parts.append('<a href="' + href + '" class="btn btn--sm' + active_cls + '">' + label + "</a> ")
    filters_html = "".join(filter_parts)

    pagination = ""
    if prev_cursor:
        pagination += (
            '<a href="/dashboard/posts?cursor='
            + _esc(prev_cursor)
            + "&status="
            + _esc(status_filter or "")
            + '" class="btn btn--sm btn--ghost">Previous</a>'
        )
    if next_cursor:
        pagination += (
            '<a href="/dashboard/posts?cursor='
            + _esc(next_cursor)
            + "&status="
            + _esc(status_filter or "")
            + '" class="btn btn--sm btn--ghost">Next</a>'
        )

    empty = (
        '<tr><td colspan="4"><div class="empty-state">'
        '<div class="empty-state__mesh"></div>'
        '<div class="empty-state__content"><h3>No posts yet</h3>'
        "<p>Create one with <code>curl</code> -- see "
        '<a href="/llms.txt">/llms.txt</a></p>'
        '<a href="/dashboard/editor" class="btn btn--primary">Create post</a>'
        "</div></div></td></tr>"
    )
    tbody = rows if rows else empty

    body = (
        '<div class="dash-header"><h2>Posts</h2>'
        '<a href="/dashboard/editor" class="btn btn--primary">New post</a></div>'
        '<div style="display:flex;gap:var(--s-2);margin-bottom:var(--s-4);flex-wrap:wrap">'
        + filters_html
        + "</div>"
        '<div class="card"><div class="table-wrap"><table>'
        "<thead><tr><th>Title</th><th>Status</th><th>Updated</th><th>Actions</th></tr></thead>"
        "<tbody>"
        + tbody
        + "</tbody></table></div>"
        + ("<div class='pagination'>" + pagination + "</div>" if pagination else "")
        + "</div>"
    )
    return render_base(
        title="Posts", csrf_token=csrf_token, active_nav="posts", body_html=body, request=request
    )


def render_editor(
    *,
    post: dict[str, Any] | None = None,
    validation_errors: list[dict[str, str]] | None = None,
    csrf_token: str,
    request: Any = None,
) -> str:
    """Render the post editor page."""
    is_edit = post is not None
    title_val = post.get("title", "") if post else ""
    body_md_val = post.get("body_md", "") if post else ""
    slug_val = post.get("slug", "") if post else ""
    excerpt_val = post.get("excerpt", "") if post else ""
    tags_val = ", ".join(post.get("tags", [])) if post else ""
    etag_val = post.get("content_hash", "") if post else ""
    post_id = post.get("id", "") if post else ""
    status = post.get("status", "") if post else ""
    revision = post.get("revision", 0) if post else 0

    status_html = ""
    if is_edit:
        status_html = (
            '<span class="badge badge--' + _esc(status) + '">' + _esc(status) + "</span> "
            '<span class="tnum" style="color:var(--text-tertiary)">v' + str(revision) + "</span>"
        )

    form_action = "/dashboard/posts/" + _esc(post_id) if is_edit else "/dashboard/posts"

    errors_html = ""
    if validation_errors:
        for err in validation_errors:
            errors_html += (
                '<div class="toast toast--error" style="display:block;position:static;margin-bottom:var(--s-2)">'
                + _esc(err.get("message", ""))
                + "</div>"
            )

    etag_field = ""
    rev_field = ""
    if is_edit:
        etag_field = '<input type="hidden" name="if_match" value="' + _esc(etag_val) + '">'
        rev_field = '<input type="hidden" name="revision" value="' + str(revision) + '">'

    publish_btn = ""
    if is_edit and status == "draft":
        publish_btn = _hx_post_btn(
            "/dashboard/posts/" + _esc(post_id) + "/publish",
            "Publish",
            csrf_token,
            cls="btn",
        )
        publish_btn = publish_btn.replace('class="btn"', 'class="btn" style="width:100%"')

    body = (
        '<div class="dash-header"><h2>' + ("Edit" if is_edit else "New") + " post " + status_html + "</h2>"
        '<div style="display:flex;gap:var(--s-2)">'
        '<a href="/dashboard/posts" class="btn btn--ghost">Back to posts</a></div></div>'
        + errors_html
        + '<form method="post" action="'
        + form_action
        + '" '
        'style="display:grid;grid-template-columns:1fr 320px;gap:var(--s-5)">'
        '<div style="display:flex;flex-direction:column;gap:var(--s-4)">'
        '<div class="field"><label for="title">Title</label>'
        '<input type="text" id="title" name="title" value="' + _esc(title_val) + '" '
        'placeholder="Post title"></div>'
        '<div class="field"><label for="body_md">Body (Markdown)</label>'
        '<textarea id="body_md" name="body_md" rows="20" '
        'placeholder="Write your post in Markdown...">' + _esc(body_md_val) + "</textarea>"
        '<span class="help">Use Markdown syntax. First H1 becomes the title if omitted.</span>'
        "</div></div>"
        '<div style="display:flex;flex-direction:column;gap:var(--s-4)">'
        '<div class="card"><h3 style="font:var(--text-title-md);margin:0 0 var(--s-4)">Settings</h3>'
        '<div class="field"><label for="slug">Slug</label>'
        '<input type="text" id="slug" name="slug" value="' + _esc(slug_val) + '" '
        'placeholder="auto-derived from title"></div>'
        '<div class="field"><label for="excerpt">Excerpt</label>'
        '<textarea id="excerpt" name="excerpt" rows="3" '
        'placeholder="Brief description">' + _esc(excerpt_val) + "</textarea></div>"
        '<div class="field"><label for="tags">Tags (comma-separated)</label>'
        '<input type="text" id="tags" name="tags" value="' + _esc(tags_val) + '" '
        'placeholder="tech, intro, guide"></div>'
        '<input type="hidden" name="csrf_token" value="'
        + _esc(csrf_token)
        + '">'
        + etag_field
        + rev_field
        + "</div>"
        '<div class="card" style="display:flex;flex-direction:column;gap:var(--s-3)">'
        '<button type="submit" class="btn btn--primary" style="width:100%">'
        + ("Update" if is_edit else "Create")
        + " draft</button>"
        + publish_btn
        + "</div></div></form>"
    )
    return render_base(
        title=("Edit" if is_edit else "New") + " post",
        csrf_token=csrf_token,
        active_nav="posts",
        body_html=body,
        request=request,
    )


def render_revisions(
    *,
    post_id: str,
    post_slug: str,
    revisions: list[dict[str, Any]],
    diff: dict[str, Any] | None = None,
    csrf_token: str,
    request: Any = None,
) -> str:
    """Render the revisions page."""
    rev_parts: list[str] = []
    for rev in revisions:
        diff_url = (
            "/dashboard/posts/"
            + _esc(post_id)
            + "/revisions?from="
            + str(rev.get("revision", ""))
            + "&to="
            + (revisions[0].get("revision", "") if revisions else "")
        )
        rev_parts.append(
            "<tr><td class='tnum'>#" + str(rev.get("revision", "?")) + "</td>"
            "<td>" + _esc(rev.get("title", "")) + "</td>"
            '<td><span class="badge badge--'
            + _esc(rev.get("status", ""))
            + '">'
            + _esc(rev.get("status", ""))
            + "</span></td>"
            "<td>" + _esc(rev.get("actor_id", "")[:8]) + "</td>"
            '<td class="tnum" style="color:var(--text-tertiary)">'
            + _esc(str(rev.get("created_at", ""))[:19])
            + "</td>"
            '<td><button class="btn btn--sm btn--ghost" hx-get="'
            + diff_url
            + '" hx-target="#diff-panel" hx-swap="innerHTML">Diff</button></td></tr>\n'
        )
    timeline = "".join(rev_parts)

    diff_html = '<p style="color:var(--text-tertiary)">Select two revisions to compare.</p>'
    if diff and diff.get("diff_unified"):
        lines = diff["diff_unified"].split("\n")
        diff_lines: list[str] = []
        for line in lines:
            cls = ""
            if line.startswith("+") and not line.startswith("+++"):
                cls = " added"
            elif line.startswith("-") and not line.startswith("---"):
                cls = " removed"
            diff_lines.append('<div class="' + cls + '">' + _esc(line) + "</div>")
        diff_html = '<div class="diff">' + "".join(diff_lines) + "</div>"
    elif diff:
        diff_html = '<p style="color:var(--text-tertiary)">No differences.</p>'

    body = (
        '<div class="dash-header"><h2>Revisions: ' + _esc(post_slug) + "</h2>"
        '<a href="/dashboard/posts/' + _esc(post_slug) + '" class="btn btn--ghost">Edit post</a></div>'
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:var(--s-4)">'
        '<div class="card"><div class="table-wrap"><table>'
        "<thead><tr><th>Rev</th><th>Title</th><th>Status</th><th>Actor</th><th>When</th><th></th></tr></thead>"
        "<tbody>" + timeline + "</tbody></table></div></div>"
        '<div class="card" id="diff-panel">'
        '<h3 style="font:var(--text-title-md);margin:0 0 var(--s-4)">Diff viewer</h3>'
        + diff_html
        + "</div></div>"
    )
    return render_base(
        title="Revisions - " + _esc(post_slug),
        csrf_token=csrf_token,
        active_nav="posts",
        body_html=body,
        request=request,
    )


def render_reviews(
    *,
    reviews: list[dict[str, Any]],
    csrf_token: str,
    request: Any = None,
) -> str:
    """Render the review queue page."""
    row_parts: list[str] = []
    for r in reviews:
        status = r.get("status", "pending_review")
        approve_url = "/dashboard/reviews/" + _esc(r.get("id", "")) + "/approve"
        reject_url = "/dashboard/reviews/" + _esc(r.get("id", "")) + "/reject"
        actions = ""
        if status == "pending_review":
            actions = (
                _hx_post_btn(approve_url, "Approve", csrf_token, "btn btn--sm btn--primary")
                + " "
                + _hx_post_btn(reject_url, "Reject", csrf_token, "btn btn--sm btn--danger")
            )
        row_parts.append(
            '<tr><td><a href="/dashboard/posts/'
            + _esc(r.get("post_id", ""))
            + '">'
            + _esc(r.get("snapshot_title", "Untitled"))
            + "</a></td>"
            '<td><span class="badge badge--' + _esc(status) + '">' + _esc(status) + "</span></td>"
            '<td class="tnum" style="color:var(--text-tertiary)">'
            + _esc(str(r.get("created_at", ""))[:19])
            + "</td>"
            '<td style="white-space:nowrap">' + actions + "</td></tr>\n"
        )
    rows = "".join(row_parts)
    empty = (
        '<tr><td colspan="4"><div class="empty-state">'
        '<div class="empty-state__mesh"></div>'
        '<div class="empty-state__content"><h3>No pending reviews</h3>'
        "<p>All posts have been reviewed.</p></div></div></td></tr>"
    )

    body = (
        '<div class="dash-header"><h2>Review queue</h2></div>'
        '<div class="card"><div class="table-wrap"><table>'
        "<thead><tr><th>Post</th><th>Status</th><th>Submitted</th><th>Actions</th></tr></thead>"
        "<tbody>" + (rows if rows else empty) + "</tbody></table></div></div>"
    )
    return render_base(
        title="Review queue", csrf_token=csrf_token, active_nav="reviews", body_html=body, request=request
    )


def render_tokens(
    *,
    tokens: list[dict[str, Any]],
    csrf_token: str,
    request: Any = None,
) -> str:
    """Render the tokens management page."""
    row_parts: list[str] = []
    for t in tokens:
        revoked = t.get("revoked_at") is not None
        badge_cls = "trashed" if revoked else "published"
        badge_txt = "revoked" if revoked else "active"
        last_used = _esc(str(t.get("last_used_at", ""))[:10]) if t.get("last_used_at") else "never"
        actions = ""
        if not revoked:
            revoke_url = "/dashboard/tokens/" + _esc(t.get("id", ""))
            actions += (
                '<button class="btn btn--sm btn--danger" hx-delete="'
                + revoke_url
                + '" '
                + _hx_headers_csrf(csrf_token)
                + ' hx-swap="none" hx-confirm="Revoke this token?">Revoke</button>'
            )
        rotate_url = "/dashboard/tokens/" + _esc(t.get("id", "")) + "/rotate"
        actions += " " + _hx_post_btn(rotate_url, "Rotate", csrf_token)
        row_parts.append(
            "<tr><td>" + _esc(t.get("label", "")) + "</td>"
            '<td><span class="badge badge--' + badge_cls + '">' + badge_txt + "</span></td>"
            '<td class="tnum">' + _esc(", ".join(t.get("scopes", []))) + "</td>"
            '<td class="tnum" style="color:var(--text-tertiary)">' + last_used + "</td>"
            '<td class="tnum">' + str(t.get("uses_count", 0)) + "</td>"
            '<td style="white-space:nowrap">' + actions + "</td></tr>\n"
        )
    rows = "".join(row_parts)
    empty = (
        '<tr><td colspan="6"><div class="empty-state">'
        '<div class="empty-state__mesh"></div>'
        '<div class="empty-state__content"><h3>No tokens yet</h3>'
        "<p>Create a token to start using the API.</p></div></div></td></tr>"
    )

    body = (
        '<div class="dash-header"><h2>Tokens &amp; links</h2>'
        '<button class="btn btn--primary" hx-get="/dashboard/tokens/new" '
        'hx-target="#create-form" hx-swap="innerHTML">Create token</button></div>'
        '<div id="create-form"></div>'
        '<div class="card"><div class="table-wrap"><table>'
        "<thead><tr><th>Label</th><th>Status</th><th>Scopes</th>"
        "<th>Last used</th><th>Uses</th><th>Actions</th></tr></thead>"
        "<tbody>" + (rows if rows else empty) + "</tbody></table></div></div>"
    )
    return render_base(
        title="Tokens", csrf_token=csrf_token, active_nav="tokens", body_html=body, request=request
    )


def render_activity(
    *,
    events: list[dict[str, Any]],
    anomalies: list[dict[str, Any]] | None = None,
    csrf_token: str,
    request: Any = None,
) -> str:
    """Render the activity feed page."""
    row_parts: list[str] = []
    for e in events:
        action = e.get("action", "")
        if "publish" in action:
            badge = "published"
        elif "create" in action:
            badge = "draft"
        elif "trash" in action:
            badge = "trashed"
        else:
            badge = "pending_review"
        row_parts.append(
            "<tr>"
            '<td class="tnum" style="color:var(--text-tertiary)">'
            + _esc(str(e.get("created_at", ""))[:19])
            + "</td>"
            "<td>" + _esc(e.get("actor_label", "Unknown")) + "</td>"
            '<td><span class="badge badge--' + badge + '">' + _esc(action) + "</span></td>"
            "<td>" + _esc(e.get("target_type", "")) + "</td>"
            '<td style="font-family:var(--font-mono);font-size:12px;color:var(--text-tertiary)">'
            + _esc(str(e.get("target_id", ""))[:12])
            + "</td></tr>\n"
        )
    rows = "".join(row_parts)
    empty = (
        '<tr><td colspan="5" style="text-align:center;color:var(--text-tertiary);padding:var(--s-6)">'
        "No activity yet</td></tr>"
    )

    anomaly_html = ""
    if anomalies:
        for a in anomalies:
            anomaly_html += (
                '<div class="card card--raised" style="margin-bottom:var(--s-2);padding:var(--s-3) var(--s-4)">'
                '<span style="color:var(--warning);font-weight:600">' + _esc(a.get("type", "")) + "</span>"
                '<span style="color:var(--text-secondary);margin-left:var(--s-2)">'
                + _esc(str(a.get("actor_label", "")))
                + "</span>"
                '<span style="color:var(--text-tertiary);margin-left:var(--s-2)">'
                + _esc(str(a.get("action", "")))
                + "</span></div>\n"
            )

    body = (
        '<div class="dash-header"><h2>Activity</h2>'
        '<a href="/v1/admin/audit/export?format=csv" class="btn btn--ghost btn--sm" '
        'target="_blank">Export CSV</a></div>'
    )
    if anomaly_html:
        body += (
            '<div style="margin-bottom:var(--s-4)">'
            '<h3 style="font:var(--text-title-md);margin:0 0 var(--s-3);color:var(--warning)">'
            "Anomaly flags</h3>" + anomaly_html + "</div>"
        )
    body += (
        '<div class="card"><div class="table-wrap"><table>'
        "<thead><tr><th>When</th><th>Actor</th><th>Action</th><th>Target</th><th>ID</th></tr></thead>"
        "<tbody>" + (rows if rows else empty) + "</tbody></table></div></div>"
    )
    return render_base(
        title="Activity", csrf_token=csrf_token, active_nav="activity", body_html=body, request=request
    )


def render_settings(
    *,
    site: dict[str, Any],
    policies: list[dict[str, Any]] | None = None,
    csrf_token: str,
    request: Any = None,
) -> str:
    """Render the settings page."""
    auto_sel = " selected" if site.get("publish_mode") == "auto" else ""
    review_sel = " selected" if site.get("publish_mode") == "require_review" else ""

    policy_list = ""
    if policies:
        items: list[str] = []
        for p in policies:
            badge_cls = "published" if p.get("enabled") else "trashed"
            badge_txt = "enabled" if p.get("enabled") else "disabled"
            items.append(
                '<li style="padding:var(--s-2) 0;border-bottom:1px solid var(--border-subtle);'
                'font:var(--text-body-sm)">'
                + _esc(p.get("name", ""))
                + ' <span class="badge badge--'
                + badge_cls
                + '">'
                + badge_txt
                + "</span></li>"
            )
        policy_list = "<ul style='list-style:none;padding:0;margin:0'>" + "".join(items) + "</ul>"
    else:
        policy_list = (
            "<p style='color:var(--text-secondary);font:var(--text-body-sm)'>No policies configured.</p>"
        )

    body = (
        '<div class="dash-header"><h2>Settings</h2></div>'
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:var(--s-4)">'
        '<div class="card">'
        '<h3 style="font:var(--text-title-md);margin:0 0 var(--s-4)">Site</h3>'
        '<form method="post" action="/dashboard/settings/site" '
        'style="display:flex;flex-direction:column;gap:var(--s-4)">'
        '<div class="field"><label for="site_name">Site name</label>'
        '<input type="text" id="site_name" name="site_name" value="' + _esc(site.get("name", "")) + '"></div>'
        '<div class="field"><label for="base_url">Base URL</label>'
        '<input type="text" id="base_url" name="base_url" value="' + _esc(site.get("base_url", "")) + '" '
        'placeholder="https://example.com"></div>'
        '<div class="field"><label for="publish_mode">Publish mode</label>'
        '<select id="publish_mode" name="publish_mode">'
        '<option value="auto"' + auto_sel + ">Auto (publish immediately)</option>"
        '<option value="require_review"' + review_sel + ">Require review</option>"
        "</select></div>"
        '<input type="hidden" name="csrf_token" value="' + _esc(csrf_token) + '">'
        '<button type="submit" class="btn btn--primary">Save settings</button>'
        "</form></div>"
        '<div class="card">'
        '<h3 style="font:var(--text-title-md);margin:0 0 var(--s-4)">Content policies</h3>'
        + policy_list
        + "</div></div>"
    )
    return render_base(
        title="Settings", csrf_token=csrf_token, active_nav="settings", body_html=body, request=request
    )
