import React from "react";
import ReactDOM from "react-dom/client";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { Connections } from "./Connections";
import { ConnectionsSignIn } from "./ConnectionsSignIn";
import { EgressCredentials } from "./EgressCredentials";
import "../index.css";

// The server serves the egress page for this path and the paths under it only.
const { pathname } = window.location;
const isEgressPage =
  pathname === "/connections/egress" ||
  pathname.startsWith("/connections/egress/");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      {isEgressPage ? (
        <EgressCredentials />
      ) : (
        <ConnectionsSignIn>
          <Connections />
        </ConnectionsSignIn>
      )}
    </ThemeProvider>
  </React.StrictMode>,
);
