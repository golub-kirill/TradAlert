import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";

import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/motion.css";
import "./styles/components.css";
import "./styles/deck.css";
import "./styles/landing.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
