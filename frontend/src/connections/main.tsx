import React from "react";
import ReactDOM from "react-dom/client";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { Connections } from "./Connections";
import { EgressCredentials } from "./EgressCredentials";
import "../index.css";

const Page = window.location.pathname.startsWith("/connections/egress")
  ? EgressCredentials
  : Connections;

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <Page />
    </ThemeProvider>
  </React.StrictMode>,
);
