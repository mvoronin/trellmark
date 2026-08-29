#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 sources CONTAINERFILE... | audit IMAGE... | audit-pod POD" >&2
    exit 2
}

containerfile_sources() {
    local containerfile image
    declare -A seen=()

    for containerfile in "$@"; do
        while IFS= read -r image; do
            if [[ -n "$image" && ! -v "seen[$image]" ]]; then
                seen["$image"]=1
                printf '%s\n' "$image"
            fi
        done < <(
            awk '
                function record_reference(reference) {
                    references[++reference_count] = reference
                }

                toupper($1) == "FROM" {
                    source_field = 2
                    while (source_field <= NF && $source_field ~ /^--/) {
                        source_field++
                    }
                    if (source_field <= NF) {
                        record_reference($source_field)
                    }
                    for (field = source_field + 1; field < NF; field++) {
                        if (toupper($field) == "AS") {
                            stages[tolower($(field + 1))] = 1
                        }
                    }
                    next
                }

                toupper($1) == "COPY" {
                    for (field = 2; field <= NF; field++) {
                        if (tolower($field) ~ /^--from=/) {
                            reference = $field
                            sub(/^[^=]*=/, "", reference)
                            record_reference(reference)
                        }
                    }
                }

                END {
                    for (reference_index = 1;
                         reference_index <= reference_count;
                         reference_index++) {
                        reference = references[reference_index]
                        if (reference !~ /^[0-9]+$/ &&
                            !(tolower(reference) in stages)) {
                            print reference
                        }
                    }
                }
            ' "$containerfile"
        )
    done
}

audit_images() {
    local image details image_id repo_digests
    local inspect_format='{{.ID}}|{{.RepoDigests}}'
    declare -A seen=()

    for image in "$@"; do
        if [[ -n "$image" && ! -v "seen[$image]" ]]; then
            seen["$image"]=1
            details="$(podman image inspect --format "$inspect_format" "$image")"
            IFS='|' read -r image_id repo_digests <<< "$details"
            printf 'Resolved image: %s image-id=%s repo-digests=%s\n' \
                "$image" "$image_id" "$repo_digests"
        fi
    done
}

audit_pod() {
    local pod="$1" output container details name image_name image_id
    local name_format='{{if not .IsInfra}}{{.Names}}{{end}}'
    local inspect_format='{{if not .IsInfra}}{{.Name}}|{{.ImageName}}|{{.Image}}{{end}}'
    local -a containers=()

    output="$(podman ps --pod --filter "pod=$pod" --format "$name_format")"
    if [[ -z "$output" ]]; then
        echo "No application containers are running in pod $pod." >&2
        return 1
    fi
    mapfile -t containers <<< "$output"

    for container in "${containers[@]}"; do
        [[ -n "$container" ]] || continue
        details="$(
            podman container inspect --format "$inspect_format" "$container"
        )"
        [[ -n "$details" ]] || continue
        IFS='|' read -r name image_name image_id <<< "$details"
        printf 'Running container: %s image=%s image-id=%s\n' \
            "$name" "$image_name" "$image_id"
    done
}

[[ $# -ge 1 ]] || usage
command="$1"
shift

case "$command" in
    sources)
        [[ $# -ge 1 ]] || usage
        containerfile_sources "$@"
        ;;
    audit)
        [[ $# -ge 1 ]] || usage
        audit_images "$@"
        ;;
    audit-pod)
        [[ $# -eq 1 ]] || usage
        audit_pod "$1"
        ;;
    *)
        usage
        ;;
esac
