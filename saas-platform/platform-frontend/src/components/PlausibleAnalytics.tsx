import Script from 'next/script'

// Privacy-friendly analytics by Plausible, self-hosted at plausible.nijho.lt.
const PLAUSIBLE_SCRIPT_URL = 'https://plausible.nijho.lt/js/pa-AEIwt-FWjemwLglbwP-ys.js'

// Only public pages are counted. Signed-in pages carry account identifiers in their paths and sit
// behind the account's own analytics consent, so their events are dropped, including after
// client-side navigation from a public page.
const PUBLIC_PATHS = ['/', '/privacy', '/terms', '/auth/login', '/auth/signup']

const INIT_SCRIPT = [
  'window.plausible=window.plausible||function(){(plausible.q=plausible.q||[]).push(arguments)},plausible.init=plausible.init||function(i){plausible.o=i||{}};',
  `plausible.init({transformRequest:function(r){return ${JSON.stringify(PUBLIC_PATHS)}.indexOf(new URL(r.u,location.href).pathname)>-1?r:null}})`,
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
