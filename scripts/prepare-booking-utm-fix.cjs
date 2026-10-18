const fs = require('fs');
const path = require('path');
const source = 'C:/Users/DELL/.codex/attachments/5dacb63e-2a37-407f-8240-b59b477fde38/pasted-text.txt';
const out = path.resolve('deliverables/booking-utm-fix');
fs.mkdirSync(out, {recursive:true});
let php = fs.readFileSync(source, 'utf8').replace(/\r\n/g, '\n');
if (!fs.existsSync(path.join(out,'original-location-snippet.txt'))) {
  fs.writeFileSync(path.join(out,'original-location-snippet.txt'), php);
}
function replaceOnce(from,to) {
  if (php.split(from).length !== 2) throw new Error('Unexpected original snippet: '+from.slice(0,60));
  php = php.replace(from,to);
}
replaceOnce("'permission_callback' => array( 'TM_Education_REST', 'verify_nonce' )", "'permission_callback' => array( 'TM_Education_REST', 'verify_booking_permission' )");
replaceOnce("public string $lead_location_label = '';", "public string $lead_location_label = '';\n\t\tpublic array $utm_values = array();");
replaceOnce("$contact['customFields'] = array(\n\t\t\t\t\tarray(", "$contact['customFields'] = array_merge(\n\t\t\t\t\tis_array( $contact['customFields'] ?? null ) ? $contact['customFields'] : array(),\n\t\t\t\t\tarray( array(");
replaceOnce("\t\t\t\t\t),\n\t\t\t\t);", "\t\t\t\t\t) )\n\t\t\t\t);");
replaceOnce("\t\t\treturn parent::upsert_contact( $contact );", `\t\t\t// Only send non-empty UTMs; an untagged booking must not erase saved values.
\t\t\t$utm_ids = array(
\t\t\t\t'utm_source' => 'vTMBPq7xdf5Fsjua0aEv',
\t\t\t\t'utm_medium' => 'trSF6KkqNAIInFRCveAP',
\t\t\t\t'utm_campaign' => 'AuU2gj8n8NuPZo3TeMv1',
\t\t\t\t'utm_content' => 'oyH2NnSISwmjTY0Av4E5',
\t\t\t\t'utm_term' => 'mZ3OEG4ZxY8rgNdA58PF',
\t\t\t);
\t\t\tforeach ( $utm_ids as $key => $id ) {
\t\t\t\t$value = $this->utm_values[ $key ] ?? '';
\t\t\t\tif ( '' !== $value ) {
\t\t\t\t\t$contact['customFields'][] = array( 'id' => $id, 'field_value' => $value );
\t\t\t\t}
\t\t\t}
\t\t\treturn parent::upsert_contact( $contact );`);
replaceOnce("\t$service = new TM_Education_Booking( $client );", `\tforeach ( array( 'utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term' ) as $key ) {
\t\t$value = $request->get_param( $key );
\t\tif ( is_string( $value ) ) {
\t\t\t$client->utm_values[ $key ] = sanitize_text_field( $value );
\t\t}
\t}
\t$consultation_type = $request->get_param( 'consultationType' );
\t$consultation_type = is_string( $consultation_type )
\t\t? TM_GHL_Config::normalize_consultation_type( $consultation_type ) : '';

\t$service = new TM_Education_Booking( $client );`);
replaceOnce("\t\t\t'endTime'    => $end_time,", "\t\t\t'endTime'    => $end_time,\n\t\t\t'consultationType' => $consultation_type,");
const js = `(function () {
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
        (/\\/education\\/v1\\/book\\/?$/.test(url.pathname) || url.searchParams.get('rest_route') === '/education/v1/book');
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
})();`;
php += `
// Add UTMs to the custom education booking request after the page scripts load.
add_action( 'wp_footer', static function () {
\tif ( ! is_page( array( 1281, 13558, 'book-consultation' ) ) ) return;
\techo '<script id="tm-education-utm-booking">';
\techo <<<'TM_EDU_UTM_JS'
${js}
TM_EDU_UTM_JS;
\techo '</script>';
}, 100 );
`;
fs.writeFileSync(path.join(out,'education-location-and-utm-replacement.txt'), php);
fs.writeFileSync(path.join(out,'booking-utm-browser.js'), js);
console.log('Prepared replacement and original backup in '+out);
