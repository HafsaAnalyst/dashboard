const fs = require('fs');
const assert = require('assert/strict');
const vm = require('vm');
const snippet = fs.readFileSync('deliverables/booking-utm-fix/education-location-and-utm-replacement.txt','utf8');
const rest = fs.readFileSync('C:/Users/DELL/Desktop/class-education-rest.txt','utf8');
assert(snippet.includes("'permission_callback' => array( 'TM_Education_REST', 'verify_booking_permission' )"));
assert(!snippet.includes('verify_nonce'));
assert(rest.includes('public static function verify_booking_permission'));
const js = snippet.split("echo <<<'TM_EDU_UTM_JS'")[1].split('TM_EDU_UTM_JS;')[0];
new vm.Script(js);
function run(search, stored, endpoint='/wp-json/education/v1/book') {
  let sent;
  const window = {
    location: {search, origin:'https://themigration.com.au',href:'https://themigration.com.au/book-consultation'+search},
    fetch: (url, options) => { sent=JSON.parse(options.body); return Promise.resolve({ok:true}); }
  };
  vm.runInNewContext(js,{window,URL,URLSearchParams,sessionStorage:{getItem:()=>stored,setItem:()=>{}}});
  window.fetch(endpoint,{method:'POST',body:JSON.stringify({consultationType:'online',leadLocation:'sydney'})});
  return sent;
}
const tagged=run('?utm_source=facebook&utm_medium=paid_social&utm_campaign=consultation&utm_content=ad&utm_term=test');
assert.deepEqual(tagged,{consultationType:'online',leadLocation:'sydney',utm_source:'facebook',utm_medium:'paid_social',utm_campaign:'consultation',utm_content:'ad',utm_term:'test'});
assert.equal(run('',JSON.stringify({utm_source:'google'})).utm_source,'google');
assert.equal(run('?utm_source=facebook',JSON.stringify({utm_campaign:'old'})).utm_campaign,undefined);
assert.equal(run('').utm_source,undefined);
assert.equal(run('?utm_source=facebook',null,'/wp-json/education/v1/availability').utm_source,undefined);
assert.equal(run('?utm_source=facebook',null,'https://example.com/education/v1/book').utm_source,undefined);
console.log('PASS: callback matches supplied plugin; embedded JavaScript parses; five UTMs, session fallback, blank inputs, unrelated requests and booking selections checked.');
console.log('PHP runtime and live WordPress booking are not tested by this check.');
