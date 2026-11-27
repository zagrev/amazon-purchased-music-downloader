import argparse
import json
import re
import subprocess
import sys


def parse_version(tag):
    if not isinstance(tag, str):
        return None
    match = re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag)
    return tuple(map(int, match.groups())) if match else None


def release_plan(tags, releases, commit):
    matching = [
        tag for tag, sha in tags.items()
        if sha == commit and parse_version(tag) is not None
    ]
    matching.extend(
        release["tag_name"] for release in releases
        if release["draft"] and release["target_commitish"] == commit
        and release["tag_name"] not in tags and parse_version(release["tag_name"]) is not None
    )
    if matching:
        tag = max(matching, key=parse_version)
        release = next((item for item in releases if item["tag_name"] == tag), None)
        return {
            "version": tag[1:],
            "reserved": tag in tags,
            "existing": release is not None,
            "published": release is not None and not release["draft"],
        }
    versions = [
        version for tag in [*tags, *(release["tag_name"] for release in releases)]
        if (version := parse_version(tag)) is not None
    ]
    if versions:
        major, minor, patch = max(versions)
        version = f"{major}.{minor}.{patch + 1}"
    else:
        version = "0.1.0"
    return {"version": version, "reserved": False, "existing": False, "published": False}


def remote_tags():
    output = subprocess.check_output(
        ["git", "ls-remote", "--tags", "origin"], encoding="utf-8",
    )
    tags = {}
    peeled = {}
    for line in output.splitlines():
        sha, ref = line.split("\t", 1)
        tag = ref.removeprefix("refs/tags/")
        if tag.endswith("^{}"):
            peeled[tag[:-3]] = sha
        else:
            tags[tag] = sha
    tags.update(peeled)
    return tags


def remote_releases(repository):
    output = subprocess.check_output(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repository}/releases"], encoding="utf-8",
    )
    pages = json.loads(output)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        raise ValueError("Expected an array of GitHub release pages.")
    return [release for page in pages for release in page]


def reserve_release(repository, commit):
    for attempt in range(20):
        plan = release_plan(remote_tags(), remote_releases(repository), commit)
        if plan["reserved"]:
            return plan
        tag = f"v{plan['version']}"
        result = subprocess.run(
            ["gh", "api", "--method", "POST", f"repos/{repository}/git/refs",
             "-f", f"ref=refs/tags/{tag}", "-f", f"sha={commit}"],
            capture_output=True, encoding="utf-8",
        )
        if result.returncode == 0:
            plan["reserved"] = True
            return plan
        tags = remote_tags()
        if tag not in tags:
            raise RuntimeError(f"Could not reserve {tag}: {result.stderr.strip()}")
        print(f"Version {tag} was reserved concurrently; retrying.", file=sys.stderr)
    raise RuntimeError("Could not reserve a release version after 20 concurrent conflicts.")


def main():
    parser = argparse.ArgumentParser(description="Reserve a patch-versioned GitHub release tag.")
    parser.add_argument("repository", help="GitHub owner/repository")
    parser.add_argument("commit", help="The exact tested commit SHA")
    args = parser.parse_args()
    print(json.dumps(reserve_release(args.repository, args.commit)))


if __name__ == "__main__":
    main()
