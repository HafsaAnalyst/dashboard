(function () {
  'use strict';
  if (window.__tmEducationUtmPatch) return;
  window.__tmEducationUtmPatch = true;
  var keys = ['utm_source','utm_medium','utm_campaign','utm_content','utm_term'];
  function readUtms() {
    var params = new URLSearchParams(window.location.search);
    var current = {};
    var hasCampaign = keys.some(function (key) { return params.has(key); });
    if (hasCampaign) {
      keys.forEach(function (key) {
        var value = (params.get(key) || '').trim();
        if (value) current[key] = value;
      });
      try { sessionStorage.setItem('tm_utms', JSON.stringify(current)); } catch (e) {}
      return current;
    }
    try {
      var stored = JSON.parse(sessionStorage.getItem('tm_utms') || '{}');
      keys.forEach(function (key) {
        if (stored && typeof stored[key] === 'string' && stored[key].trim()) current[key] = stored[key].trim();
      });
    } catch (e) {}
    return current;
  }
  readUtms();
  var previousFetch = window.fetch;
  window.fetch = function (input, init) {
    var next = init;
    try {
      var raw = typeof input === 'string' ? input : (input && input.url) || String(input);
      var url = new URL(raw, window.location.href);
      var isBooking = url.origin === window.location.origin &&
        (/\/education\/v1\/book\/?$/.test(url.pathname) || url.searchParams.get('rest_route') === '/education/v1/book');
      var method = String((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      if (isBooking && method === 'POST' && init && typeof init.body === 'string') {
        var body = JSON.parse(init.body);
        if (body && typeof body === 'object' && !Array.isArray(body)) {
          var utms = readUtms();
          keys.forEach(function (key) { if (utms[key]) body[key] = utms[key]; });
          next = Object.assign({}, init, {body: JSON.stringify(body)});
        }
      }
    } catch (e) {}
    return previousFetch.call(this, input, next);
  };
})();