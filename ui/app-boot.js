(() => {
  "use strict";

  if (!("serviceWorker" in navigator)) return;
  window.addEventListener(
    "load",
    () => {
      navigator.serviceWorker.register("/service-worker.js", {scope: "/"}).catch(
        () => {
          // The live application remains fully usable if registration fails.
        },
      );
    },
    {once: true},
  );
})();
