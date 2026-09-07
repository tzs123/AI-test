USERS = {
    "admin": {"role": "admin"},
    "tester": {"role": "tester"},
    "viewer": {"role": "viewer"}
}


def check_permission(user, action):

    role = USERS.get(user, {}).get("role")

    if role == "admin":
        return True

    if role == "tester" and action in ["run", "view"]:
        return True

    if role == "viewer" and action == "view":
        return True

    return False
