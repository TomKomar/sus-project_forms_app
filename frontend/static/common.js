/* ---------------------------------------------------------------------------
 * common.js
 *
 * Shared browser utilities used by all pages.
 *
 * Responsibilities:
 * - JSON API helpers (GET/POST/PUT/PATCH/DELETE) with consistent error handling
 * - Small DOM factory helper `el()` for safe element construction
 * - Accessible modal dialog helpers (showModal/hideModal)
 *
 * This file intentionally exposes a small, stable API on `globalThis` so that
 * inline scripts (e.g., index.html/register.html) and app.js can call the same
 * functions without a bundler.
 * ------------------------------------------------------------------------- */
/* eslint-env browser */

(function () {
  'use strict';

  /**
   * Attempt to parse JSON response; returns null on failure.
   * @param {Response} res
   * @returns {Promise<any|null>}
   */
  async function safeJson(res) {
    try {
      return await res.json();
    } catch {
      return null;
    }
  }

  /**
   * Build a user-friendly error message from a failed API response.
   * @param {Response} res
   * @returns {Promise<string>}
   */
  async function errorMessage(res) {
    const data = await safeJson(res);
    return (data && data.detail) || res.statusText || `HTTP ${res.status}`;
  }

  /**
   * Issue a JSON request with credentials included (cookie sessions).
   * @param {string} url
   * @param {RequestInit} init
   * @returns {Promise<any>}
   */
  async function requestJson(url, init) {
    const res = await fetch(url, { credentials: 'include', ...init });
    if (!res.ok) {
      throw new Error(await errorMessage(res));
    }
    // Some endpoints may return empty bodies; treat as {}.
    const data = await safeJson(res);
    return data === null ? {} : data;
  }

  /** @param {string} url */
  function apiGet(url) {
    return requestJson(url, { method: 'GET' });
  }

  /** @param {string} url @param {any} body */
  function apiPost(url, body) {
    return requestJson(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
  }

  /** @param {string} url @param {any} body */
  function apiPut(url, body) {
    return requestJson(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
  }

  /** @param {string} url @param {any} body */
  function apiPatch(url, body) {
    return requestJson(url, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
  }

  /** @param {string} url */
  function apiDelete(url) {
    return requestJson(url, { method: 'DELETE' });
  }

  /**
   * DOM node factory.
   *
   * Supports:
   * - {class, html} special keys
   * - on* event handlers (e.g., onclick)
   * - normal attributes including aria-* and data-*
   *
   * @param {string} tag
   * @param {Record<string, any>} [attrs]
   * @param  {...any} children
   * @returns {HTMLElement}
   */
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);

    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined) continue;

      if (key === 'class') {
        node.className = String(value);
      } else if (key === 'html') {
        node.innerHTML = String(value);
      } else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2), value);
      } else if (key === 'dataset' && typeof value === 'object') {
        for (const [dk, dv] of Object.entries(value)) {
          node.dataset[dk] = String(dv);
        }
      } else if (typeof value === 'boolean') {
        if (value) node.setAttribute(key, '');
      } else {
        node.setAttribute(key, String(value));
      }
    }

    for (const child of children) {
      if (child === null || child === undefined) continue;
      if (typeof child === 'string') node.appendChild(document.createTextNode(child));
      else node.appendChild(child);
    }

    return node;
  }

  // -----------------------------------------------------------------------
  // Modal helpers (accessible, keyboard friendly)
  // -----------------------------------------------------------------------

  /** @type {HTMLElement|null} */
  let lastFocused = null;

  /** @param {KeyboardEvent} e */
  function handleEscape(e) {
    if (e.key === 'Escape') hideModal();
  }

  /** Keep tab focus within the modal dialog. */
  function trapFocus(modalRoot) {
    const focusables = modalRoot.querySelectorAll(
      'a[href], button:not([disabled]), textarea, input, select, [tabindex]:not([tabindex="-1"])'
    );
    if (!focusables.length) return;

    const first = focusables[0];
    const last = focusables[focusables.length - 1];

    modalRoot.addEventListener('keydown', (e) => {
      if (e.key !== 'Tab') return;
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    });
  }

  /**
   * Show a modal dialog.
   * @param {string} title
   * @param {HTMLElement} bodyNode
   * @param {Function} [onOk]
   */
  function showModal(title, bodyNode, onOk) {
    const backdrop = document.getElementById('modalBackdrop');
    const host = document.getElementById('modal');
    if (!backdrop || !host) return;

    lastFocused = /** @type {HTMLElement|null} */ (document.activeElement);

    const titleId = 'modalTitle';

    const closeBtn = el(
      'button',
      {
        class: 'danger',
        type: 'button',
        'aria-label': 'Close dialog',
        onclick: () => hideModal()
      },
      'Close'
    );

    const okBtn = el(
      'button',
      {
        type: 'button',
        onclick: async () => {
          if (onOk) await onOk();
          hideModal();
        }
      },
      'OK'
    );

    const dialog = el(
      'div',
      { class: 'card modal', role: 'document', 'aria-labelledby': titleId },
      el(
        'div',
        { class: 'row', style: 'justify-content:space-between; align-items:center; margin-bottom:10px;' },
        el('h2', { id: titleId }, title),
        closeBtn
      ),
      bodyNode,
      el('div', { class: 'row', style: 'justify-content:flex-end; margin-top:12px;' }, okBtn)
    );

    host.innerHTML = '';
    host.appendChild(dialog);

    // Display & accessibility wiring
    backdrop.setAttribute('aria-hidden', 'false');
    backdrop.style.display = 'flex';

    // Close when clicking outside dialog
    backdrop.onclick = (e) => {
      if (e.target === backdrop) hideModal();
    };

    document.addEventListener('keydown', handleEscape);

    // Focus and trap
    closeBtn.focus();
    trapFocus(dialog);
  }

  function hideModal() {
    const backdrop = document.getElementById('modalBackdrop');
    const host = document.getElementById('modal');
    if (!backdrop || !host) return;

    backdrop.style.display = 'none';
    backdrop.setAttribute('aria-hidden', 'true');
    backdrop.onclick = null;
    host.innerHTML = '';

    document.removeEventListener('keydown', handleEscape);

    if (lastFocused && typeof lastFocused.focus === 'function') {
      lastFocused.focus();
    }
    lastFocused = null;
  }

  // Ensure the modal is hidden on initial load.
  window.addEventListener('DOMContentLoaded', () => {
    const b = document.getElementById('modalBackdrop');
    if (b) {
      b.style.display = 'none';
      b.setAttribute('aria-hidden', 'true');
    }
  });

  // Export stable API to globalThis (used by app.js and inline scripts).
  try {
    globalThis.apiGet = apiGet;
    globalThis.apiPost = apiPost;
    globalThis.apiPut = apiPut;
    globalThis.apiPatch = apiPatch;
    globalThis.apiDelete = apiDelete;
    globalThis.apiDel = apiDelete; // backwards-compatible alias

    globalThis.el = el;
    globalThis.showModal = showModal;
    globalThis.hideModal = hideModal;
  } catch {
    // Ignore environments without globalThis (not expected in browsers).
  }
})();
