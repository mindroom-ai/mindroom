import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { Connections } from "./Connections";
import { ConnectionsSignIn } from "./ConnectionsSignIn";
import { EgressCredentials } from "./EgressCredentials";
import "@fontsource-variable/hanken-grotesk";
import "@fontsource-variable/jetbrains-mono";
import "../index.css";

// The server serves the egress page for this path and the paths under it only.
const isEgressPage = (pathname: string) =>
  pathname === "/connections/egress" ||
  pathname.startsWith("/connections/egress/");

/** The page for the current address, which links between the pages change in place. */
function Page() {
  const [pathname, setPathname] = useState(window.location.pathname);
  useEffect(() => {
    const follow = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", follow);
    return () => window.removeEventListener("popstate", follow);
  }, []);
  return isEgressPage(pathname) ? <EgressCredentials /> : <Connections />;
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <ConnectionsSignIn>
        <Page />
      </ConnectionsSignIn>
    </ThemeProvider>
  </React.StrictMode>,
);
