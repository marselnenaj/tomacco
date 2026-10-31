// Injected by the layout lab before page scripts run: records the page load's
// uncaught errors, unhandled rejections and failed requests for
// preview.errors. It observes only and changes no result the page sees.
(() => {
  if (window.__tomaccoPageErrors) return;
  const LIMIT = 200;
  const errors = [], requests = [];
  let dropped = 0;
  const now = () => Math.round(performance.now());
  const add = (list, entry) => { if (list.length < LIMIT) list.push(entry); else dropped++; };
  const text = value => { try { return String(value && value.message !== undefined ? value.message : value).slice(0, 2000); } catch (e) { return '(unprintable)'; } };
  const stack = value => (value && typeof value.stack === 'string' ? value.stack.slice(0, 4000) : '');
  const absolute = url => { try { return new URL(String(url), location.href).href; } catch (e) { return String(url); } };
  addEventListener('error', event => {
    const target = event.target;
    if (target && target !== window && target.tagName) {
      // An element's resource did not load: an image, script, style sheet or media.
      add(requests, {kind: 'element', type: target.tagName.toLowerCase(), url: absolute(target.currentSrc || target.src || target.href || ''), status: 0, error: 'load failed', time: now()});
    } else {
      add(errors, {kind: 'exception', message: text(event.error !== undefined && event.error !== null ? event.error : event.message), source: event.filename || '', line: event.lineno || 0, column: event.colno || 0, stack: stack(event.error), time: now()});
    }
  }, true);
  addEventListener('unhandledrejection', event => {
    add(errors, {kind: 'unhandled-rejection', message: text(event.reason), source: '', line: 0, column: 0, stack: stack(event.reason), time: now()});
  });
  const fetch = window.fetch;
  if (fetch) {
    window.fetch = function (input, init) {
      const url = absolute(input && input.url !== undefined ? input.url : input);
      const method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      return fetch.apply(this, arguments).then(response => {
        if (!response.ok && response.type !== 'opaque') add(requests, {kind: 'fetch', method, url: response.url || url, status: response.status, error: response.statusText || '', time: now()});
        return response;
      }, error => {
        if (!(error && error.name === 'AbortError')) add(requests, {kind: 'fetch', method, url, status: 0, error: text(error), time: now()});
        throw error;
      });
    };
  }
  const open = XMLHttpRequest.prototype.open, send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__tomaccoRequest = {method: String(method).toUpperCase(), url: absolute(url)};
    return open.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    const xhr = this, request = xhr.__tomaccoRequest || {method: 'GET', url: ''};
    let aborted = false;
    xhr.addEventListener('abort', () => { aborted = true; });
    xhr.addEventListener('loadend', () => {
      if (aborted) return;
      if (xhr.status === 0) add(requests, {kind: 'xhr', method: request.method, url: request.url, status: 0, error: 'network error', time: now()});
      else if (xhr.status >= 400) add(requests, {kind: 'xhr', method: request.method, url: request.url, status: xhr.status, error: xhr.statusText || '', time: now()});
    });
    try {
      return send.apply(this, arguments);
    } catch (error) {
      // A synchronous request throws instead of ending.
      add(requests, {kind: 'xhr', method: request.method, url: request.url, status: 0, error: text(error), time: now()});
      aborted = true;
      throw error;
    }
  };
  Object.defineProperty(window, '__tomaccoPageErrors', {value: Object.freeze({
    report() {
      const failed = requests.map(entry => Object.assign({}, entry));
      // HTTP errors of everything else (CSS images, fonts, the document itself)
      // from Resource Timing; an element's failed load gains its status here.
      const seen = new Set(failed.filter(e => e.kind !== 'element').map(e => e.url));
      const timings = performance.getEntriesByType('resource').concat(performance.getEntriesByType('navigation'));
      for (const timing of timings) {
        const status = timing.responseStatus || 0;
        if (status < 400 || seen.has(timing.name)) continue;
        const element = failed.find(e => e.kind === 'element' && e.url === timing.name && !e.status);
        if (element) { element.status = status; element.error = 'HTTP ' + status; continue; }
        if (timing.initiatorType === 'fetch' || timing.initiatorType === 'xmlhttprequest') continue;
        add(failed, {kind: timing.entryType === 'navigation' ? 'document' : 'resource', type: timing.initiatorType || '', url: timing.name, status, error: 'HTTP ' + status, time: Math.round(timing.startTime)});
        seen.add(timing.name);
      }
      return {errors: errors.slice(), failed_requests: failed, dropped};
    }
  }), configurable: false});
})();
