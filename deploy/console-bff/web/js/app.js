// Alpine root component. Merges the per-domain modules (window.ocXxx) into one
// object. Must be loaded AFTER every app.<domain>.js and BEFORE Alpine (defer).
//
// We merge with Object.defineProperties(getOwnPropertyDescriptors) — NOT
// Object.assign — because several modules define getters (filteredTenants,
// groupedHosts, nameError, …). Object.assign would *invoke* each getter once
// and copy its return value as a static property, breaking Alpine reactivity.
// Copying descriptors keeps getters as getters so Alpine re-evaluates them.
function mergeModules(...mods) {
  const target = {};
  for (const m of mods) {
    // Defensive: a module whose <script> failed to load (404/403/网络) is
    // undefined here. getOwnPropertyDescriptors(undefined) throws, which would
    // crash the WHOLE Alpine component (blank console). Skip missing modules so
    // one absent file degrades to "that tab is empty", not "everything dead".
    if (!m) {
      console.warn(
        "[app] a module failed to load — skipped (some tab may be limited)",
      );
      continue;
    }
    Object.defineProperties(target, Object.getOwnPropertyDescriptors(m));
  }
  return target;
}

function app() {
  return mergeModules(
    window.ocCore,
    window.ocTenants,
    window.ocHosts,
    window.ocMigrations,
    window.ocBackups,
    window.ocTemplates,
    window.ocSkills,
    window.ocGroups,
    window.ocMonitoring,
    window.ocFormat,
    window.ocEdge,
    window.ocAudit,
    window.ocTraces,
    window.ocLogs,
    window.ocRsa,
  );
}
