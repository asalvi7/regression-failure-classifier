// DC Runtime — implements x-dc template engine for claude.ai/design components
(function () {
  'use strict';

  // ─── Base class ─────────────────────────────────────────────────────────
  window.DCLogic = class DCLogic {
    constructor() { this._runtime = null; }

    setState(updater) {
      if (typeof updater === 'function') {
        this.state = { ...this.state, ...updater(this.state) };
      } else {
        this.state = { ...this.state, ...updater };
      }
      if (this._runtime) this._runtime.render();
    }

    componentDidMount() {}
    renderVals() { return {}; }
  };

  // ─── Runtime ─────────────────────────────────────────────────────────────
  class DCRuntime {
    constructor(component, template, mountPoint) {
      this._component = component;
      this._template = template;
      this._mount = mountPoint;
      component._runtime = this;
    }

    _eval(expr, scope) {
      try {
        const keys = Object.keys(scope);
        const vals = Object.values(scope);
        return new Function(...keys, `"use strict"; return (${expr.trim()});`)(...vals);
      } catch (_) {
        return undefined;
      }
    }

    _interpolate(str, scope) {
      return str.replace(/\{\{([\s\S]+?)\}\}/g, (_, expr) => {
        const v = this._eval(expr, scope);
        if (v == null || typeof v === 'function') return '';
        return String(v);
      });
    }

    _resolve(str, scope) {
      const m = str && str.match(/^\{\{([\s\S]+?)\}\}$/);
      if (m) return this._eval(m[1], scope);
      return this._interpolate(str || '', scope);
    }

    _processNode(node, scope) {
      // Text node
      if (node.nodeType === Node.TEXT_NODE) {
        return document.createTextNode(this._interpolate(node.textContent, scope));
      }
      if (node.nodeType !== Node.ELEMENT_NODE) return node.cloneNode(false);

      const tag = node.tagName.toLowerCase();

      // sc-if
      if (tag === 'sc-if') {
        if (!this._resolve(node.getAttribute('value') || '', scope)) return null;
        return this._children(node, scope);
      }

      // sc-for
      if (tag === 'sc-for') {
        const list = this._resolve(node.getAttribute('list') || '', scope);
        const as = node.getAttribute('as') || 'item';
        if (!Array.isArray(list)) return null;
        const frag = document.createDocumentFragment();
        for (const item of list) {
          frag.appendChild(this._children(node, { ...scope, [as]: item }));
        }
        return frag;
      }

      // Regular element
      const el = document.createElement(tag);
      const hoverStyle = node.getAttribute('style-hover');
      const baseStyles = {};

      for (const attr of node.attributes) {
        const { name, value } = attr;
        if (name.startsWith('hint-') || name === 'style-hover') continue;

        // Event handlers
        const evtMatch = name.match(/^on([A-Za-z]+)$/);
        if (evtMatch) {
          const handler = this._resolve(value, scope);
          if (typeof handler === 'function') {
            let evt = evtMatch[1].toLowerCase();
            // React-like onChange → input for text/range inputs so updates fire immediately
            if (evt === 'change' && tag === 'input') evt = 'input';
            el.addEventListener(evt, handler);
          }
          continue;
        }

        el.setAttribute(name, this._interpolate(value, scope));
      }

      // Hover behaviour
      if (hoverStyle) {
        const parsePairs = (s) =>
          s.split(';').filter(p => p.trim()).map(p => {
            const i = p.indexOf(':');
            return i < 0 ? null : [p.slice(0, i).trim(), p.slice(i + 1).trim()];
          }).filter(Boolean);

        el.addEventListener('mouseenter', () => {
          for (const [prop, val] of parsePairs(hoverStyle)) {
            baseStyles[prop] = el.style.getPropertyValue(prop);
            el.style.setProperty(prop, val);
          }
        });
        el.addEventListener('mouseleave', () => {
          for (const [prop] of parsePairs(hoverStyle)) {
            const orig = baseStyles[prop];
            if (orig) el.style.setProperty(prop, orig);
            else el.style.removeProperty(prop);
          }
        });
      }

      // Children
      el.appendChild(this._children(node, scope));

      // Set form element value AFTER children so <select> options exist
      if ((tag === 'input' || tag === 'select') && node.hasAttribute('value')) {
        const v = this._resolve(node.getAttribute('value'), scope);
        if (v != null) el.value = String(v);
      }

      return el;
    }

    _children(node, scope) {
      const frag = document.createDocumentFragment();
      for (const child of node.childNodes) {
        const p = this._processNode(child, scope);
        if (p != null) frag.appendChild(p);
      }
      return frag;
    }

    render() {
      // Track focused text input to restore after re-render
      const focused = document.activeElement;
      let restoreInfo = null;
      if (focused && this._mount.contains(focused) &&
          (focused.tagName === 'INPUT' && focused.type === 'text')) {
        restoreInfo = { value: focused.value, ss: focused.selectionStart, se: focused.selectionEnd };
      }

      const vals = this._component.renderVals();
      const frag = document.createDocumentFragment();
      for (const child of this._template.childNodes) {
        const p = this._processNode(child, vals);
        if (p) frag.appendChild(p);
      }
      this._mount.innerHTML = '';
      this._mount.appendChild(frag);

      // Restore text input focus/cursor
      if (restoreInfo) {
        const input = this._mount.querySelector('input[type="text"]');
        if (input) {
          input.focus();
          try { input.setSelectionRange(restoreInfo.ss, restoreInfo.se); } catch (_) {}
        }
      }
    }
  }

  // ─── Bootstrap ───────────────────────────────────────────────────────────
  function boot() {
    const xdc = document.querySelector('x-dc');
    if (!xdc) return;

    // Move <helmet> children into <head>
    const helmet = xdc.querySelector('helmet');
    if (helmet) {
      for (const child of [...helmet.childNodes]) document.head.appendChild(child);
      helmet.remove();
    }

    // Evaluate component class from <script type="text/x-dc">
    const scriptEl = xdc.querySelector('script[type="text/x-dc"]');
    let ComponentClass = null;
    if (scriptEl) {
      try {
        ComponentClass = new Function('DCLogic', scriptEl.textContent + '\nreturn Component;')(window.DCLogic);
      } catch (e) {
        console.error('[DC] Component parse error:', e);
      }
      scriptEl.remove();
    }
    if (!ComponentClass) return;

    // Capture remaining x-dc content as the template
    const template = document.createDocumentFragment();
    for (const child of [...xdc.childNodes]) template.appendChild(child.cloneNode(true));

    // Mount point
    const mount = document.createElement('div');
    document.body.innerHTML = '';
    document.body.appendChild(mount);

    const component = new ComponentClass();
    const runtime = new DCRuntime(component, template, mount);

    runtime.render();
    component.componentDidMount();
    runtime.render();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
