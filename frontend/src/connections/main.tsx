import React from "react";
import ReactDOM from "react-dom/client";
import { ThemeProvider } from "@/contexts/ThemeContext";
import { Connections } from "./Connections";
import { ConnectionsSignIn } from "./ConnectionsSignIn";
import "../index.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ThemeProvider>
      <ConnectionsSignIn>
        <Connections />
      </ConnectionsSignIn>
    </ThemeProvider>
  </React.StrictMode>,
);
