import Script from 'next/script'

// Privacy-friendly analytics by Plausible, self-hosted at plausible.nijho.lt. Only public pages render
// this component; signed-in pages sit behind the account's own analytics consent.
const PLAUSIBLE_SCRIPT_URL = 'https://plausible.nijho.lt/js/pa-AEIwt-FWjemwLglbwP-ys.js'

// The script stays loaded after client-side navigation into the dashboard, so events are also filtered:
// only public paths are sent, with campaign parameters but no other query or fragment (sign-in links carry
// hosted instance addresses there), and referrers from MindRoom hosts, which name instances, are dropped.
const PUBLIC_PATHS = ['/', '/privacy', '/terms', '/auth/login', '/auth/signup']
const CAMPAIGN_PARAMS = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_content', 'utm_term', 'ref', 'source']

const TRANSFORM_REQUEST = `function(r){
var u=new URL(r.u,location.href);
if(${JSON.stringify(PUBLIC_PATHS)}.indexOf(u.pathname)<0)return null;
var q=new URLSearchParams();
u.searchParams.forEach(function(v,k){if(${JSON.stringify(CAMPAIGN_PARAMS)}.indexOf(k)>-1)q.append(k,v)});
var s=q.toString();
r.u=u.origin+u.pathname+(s?'?'+s:'');
if(r.r){try{var h=new URL(r.r).hostname;if(h==='mindroom.chat'||h.slice(-14)==='.mindroom.chat')r.r=null}catch(e){r.r=null}}
return r}`

const INIT_SCRIPT = [
  'window.plausible=window.plausible||function(){(plausible.q=plausible.q||[]).push(arguments)},plausible.init=plausible.init||function(i){plausible.o=i||{}};',
  `plausible.init({logging:false,transformRequest:${TRANSFORM_REQUEST}})`,
].join('')

export function PlausibleAnalytics() {
  return (
    <>
      <Script src={PLAUSIBLE_SCRIPT_URL} strategy="afterInteractive" />
      <Script id="plausible-init" strategy="afterInteractive">
        {INIT_SCRIPT}
      </Script>
    </>
  )
}
