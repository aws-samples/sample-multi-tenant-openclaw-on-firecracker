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
    window.ocLogs,
    window.ocFormat,
  );
}
