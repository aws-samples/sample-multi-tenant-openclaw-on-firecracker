ALIAS_CONFIG_KEYS = ("code_sha256", "environment", "dead_letter", "layers")


def _carries_nothing(config):
    """A config that normalised to entirely empty tells us nothing.

    Guarding only against None is not enough: if the supplied observation uses different key names
    than the normaliser expects, every field comes back None/{}/[] and the two sides then compare
    EQUAL. That reads as "no divergence" when the truth is "nothing was understood" — the same
    false-green this check was fixed for, one level deeper.
    """
    if not config:
        return True
    return not any(config.get(key) for key in ALIAS_CONFIG_KEYS)


def compare_alias_versions(latest, aliases, versions):
    rows, failures, comparable_pairs = [], [], 0
    for alias in aliases:
        config = versions.get(alias["version"])
        missing = []
        if latest is None:
            missing.append("$LATEST")
        elif _carries_nothing(latest):
            missing.append("$LATEST carried no readable field")
        if config is None:
            missing.append("published version %s" % alias["version"])
        elif _carries_nothing(config):
            missing.append("version %s carried no readable field" % alias["version"])
        comparable = not missing
        differences = []
        if comparable:
            comparable_pairs += 1
            differences = [key for key in ALIAS_CONFIG_KEYS
                           if latest.get(key) != config.get(key)]
            if differences:
                failures.append("%s differs in %s" %
                                (alias["alias"], ",".join(differences)))
        rows.append({
            "alias": alias["alias"], "version": alias["version"],
            "latest": latest, "published": config, "differences": differences,
            "comparable": comparable,
            "not_comparable_reason": None if comparable else "missing " + ", ".join(missing),
        })
    return rows, failures, comparable_pairs
