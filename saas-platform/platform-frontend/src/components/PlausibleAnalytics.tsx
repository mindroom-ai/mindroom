import Script from 'next/script'

// Privacy-friendly analytics by Plausible, self-hosted at plausible.nijho.lt.
const PLAUSIBLE_SCRIPT_URL = 'https://plausible.nijho.lt/js/pa-AEIwt-FWjemwLglbwP-ys.js'

export function PlausibleAnalytics() {
  return (
    <>
      <Script src={PLAUSIBLE_SCRIPT_URL} strategy="afterInteractive" />
      <Script id="plausible-init" strategy="afterInteractive">
        {'window.plausible=window.plausible||function(){(plausible.q=plausible.q||[]).push(arguments)},plausible.init=plausible.init||function(i){plausible.o=i||{}};plausible.init()'}
      </Script>
    </>
  )
}
