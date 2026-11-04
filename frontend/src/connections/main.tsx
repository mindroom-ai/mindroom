import React, { useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { Connections } from "./Connections";
import { ConnectionsSignIn } from "./ConnectionsSignIn";
import { Contacts } from "./Contacts";
import { EgressCredentials } from "./EgressCredentials";
import "@fontsource-variable/hanken-grotesk";
import "@fontsource-variable/jetbrains-mono";
import "../index.css";

// The server serves a page for its path and the paths under it only.
const isPage = (pathname: string, page: string) =>
  pathname === `/connections/${page}` ||
  pathname.startsWith(`/connections/${page}/`);

/** The page for the current address, which links between the pages change in place. */
function Page() {
  const [pathname, setPathname] = useState(window.location.pathname);
  useEffect(() => {
    const follow = () => setPathname(window.location.pathname);
    window.addEventListener("popstate", follow);
    return () => window.removeEventListener("popstate", follow);
  }, []);
  if (isPage(pathname, "egress")) return <EgressCredentials />;
  if (isPage(pathname, "contacts")) return <Contacts />;
  return <Connections />;
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
