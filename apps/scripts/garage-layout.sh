#!/usr/bin/env bash

set -euo pipefail

: "${GARAGE_NAMESPACE:=garage}"
: "${GARAGE_STATEFULSET:=garage}"
: "${GARAGE_ADMIN_POD:=garage-0}"
: "${GARAGE_ZONE:=dc1}"
: "${GARAGE_CAPACITY:=1G}"

garage() {
    kubectl exec \
        -n "$GARAGE_NAMESPACE" \
        "$GARAGE_ADMIN_POD" \
        -- /garage "$@"
}

echo "==> Garage configuration"
echo "Namespace:  $GARAGE_NAMESPACE"
echo "StatefulSet: $GARAGE_STATEFULSET"
echo "Admin pod:  $GARAGE_ADMIN_POD"
echo "Zone:       $GARAGE_ZONE"
echo "Capacity:   $GARAGE_CAPACITY"
echo

replicas="$(
    kubectl get statefulset "$GARAGE_STATEFULSET" \
        -n "$GARAGE_NAMESPACE" \
        -o jsonpath='{.spec.replicas}'
)"

if [[ -z "$replicas" || "$replicas" -lt 1 ]]; then
    echo "No Garage replicas found." >&2
    exit 1
fi

echo "==> Found $replicas Garage replicas"

for ((i = 0; i < replicas; i++)); do
    pod="garage-$i"

    node_id="$(
        kubectl exec \
            -n "$GARAGE_NAMESPACE" \
            "$pod" \
            -- /garage node id -q
    )"

    echo "Assigning $pod ($node_id) -> zone=$GARAGE_ZONE capacity=$GARAGE_CAPACITY"

    garage layout assign \
        --zone "$GARAGE_ZONE" \
        --capacity "$GARAGE_CAPACITY" \
        "$node_id"
done

echo
echo "==> Staged layout"
garage layout show

current_version="$(
    garage layout show |
        sed -n 's/^Current cluster layout version: \([0-9][0-9]*\)$/\1/p'
)"

if [[ -z "$current_version" ]]; then
    echo "Could not determine current layout version." >&2
    exit 1
fi

next_version=$((current_version + 1))

echo
echo "==> Applying layout version $next_version"

garage layout apply \
    --version "$next_version"

echo
echo "==> Final status"
garage status
