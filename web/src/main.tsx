import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, useSyncExternalStore } from "react";
import { createRoot } from "react-dom/client";
import { Toaster } from "sonner";
import App from "./App";
import "./index.css";
import { queryClient } from "./lib/queries";

// 旧版控制台的地址是 #videos 这种，换成 #/videos
if (/^#[a-z]+$/.test(location.hash)) history.replaceState(null, "", "#/" + location.hash.slice(1));

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
      <ThemedToaster />
    </QueryClientProvider>
  </StrictMode>,
);

function subscribeTheme(cb: () => void) {
  const mo = new MutationObserver(cb);
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  return () => mo.disconnect();
}

/** 提示条跟着页面的深浅色。 */
function ThemedToaster() {
  const dark = useSyncExternalStore(subscribeTheme, () => document.documentElement.classList.contains("dark"));
  return <Toaster position="bottom-right" richColors closeButton theme={dark ? "dark" : "light"} toastOptions={{ style: { fontFamily: "var(--font-sans)" } }} />;
}
