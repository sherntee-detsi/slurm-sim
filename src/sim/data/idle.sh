INSTANCE_STARTED=0
cleanup() {
    if [ -n "${SLEEP_PID:-}" ]; then
        kill "$SLEEP_PID" 2>/dev/null || true
        wait "$SLEEP_PID" 2>/dev/null || true
    fi
    if [ -n "${SSH_PID:-}" ]; then
        kill "$SSH_PID" 2>/dev/null || true
        wait "$SSH_PID" 2>/dev/null || true
    fi
    if [ "$INSTANCE_STARTED" = 1 ]; then
        apptainer instance stop "$INSTANCE_NAME" || true
    fi
    if [ -n "${READY_FILE:-}" ]; then
        rm -f -- "$READY_FILE" "$SSH_READY_FILE"
    fi
    if [ -n "${SSH_RUNTIME:-}" ]; then
        rm -f -- "$SSH_RUNTIME/sshd.py" "$SSH_RUNTIME/sshd_config"
        rmdir -- "$SSH_RUNTIME" 2>/dev/null || true
    fi
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

if [ -n "$IMAGE" ]; then
    : "${SLURM_JOB_ID:?SLURM_JOB_ID is not set}"
    INSTANCE_NAME="sim-${SLURM_JOB_ID}"
    READY_FILE="$READY_DIR/$INSTANCE_NAME"
    SSH_READY_FILE="$READY_DIR/$INSTANCE_NAME.ssh.json"
    if [ -n "$APPTAINER_MODULE" ]; then
        module load "$APPTAINER_MODULE" || exit $?
    fi
    START_FLAGS=("${FLAGS[@]}")
    if [ "$SSH_ENABLED" = 1 ]; then
        SSH_RUNTIME="$READY_DIR/.$INSTANCE_NAME.ssh"
        (umask 077; mkdir -p -- "$SSH_RUNTIME" &&
            printf '%s\n' "$SSH_SOURCE" > "$SSH_RUNTIME/sshd.py") || exit $?
        START_FLAGS+=(--bind "$HOME:$HOME")
    fi
    apptainer instance start "${START_FLAGS[@]}" "$IMAGE" "$INSTANCE_NAME" || exit $?
    INSTANCE_STARTED=1
    (umask 077; printf '%s\n' "$SLURM_JOB_ID" > "$READY_FILE") || exit $?
    if [ "$SSH_ENABLED" = 1 ]; then
        rm -f -- "$SSH_READY_FILE"
        JOIN_FLAGS=()
        if ! apptainer exec "instance://$INSTANCE_NAME" /usr/bin/python3 -c pass >/dev/null 2>&1; then
            JOIN_FLAGS+=(--userns)
        fi
        apptainer exec "${JOIN_FLAGS[@]}" "instance://$INSTANCE_NAME" /usr/bin/python3 \
            "$SSH_RUNTIME/sshd.py" "$SLURM_JOB_ID" "$SSH_READY_FILE" \
            "$SSH_RUNTIME" "$KEY_DIR" "$HOME/.ssh/authorized_keys" \
            "${JOIN_FLAGS:+userns}" &
        SSH_PID=$!
        for ((attempt = 0; attempt < 30; attempt++)); do
            [ -s "$SSH_READY_FILE" ] && break
            if ! kill -0 "$SSH_PID" 2>/dev/null; then
                echo "Container SSH failed to start." >&2
                exit 1
            fi
            sleep 1
        done
        if [ ! -s "$SSH_READY_FILE" ]; then
            echo "Timed out waiting for container SSH." >&2
            exit 1
        fi
    fi
fi

while true; do
    if [ -n "${SSH_PID:-}" ] && ! kill -0 "$SSH_PID" 2>/dev/null; then
        echo "Container SSH exited unexpectedly." >&2
        exit 1
    fi
    sleep 30 &
    SLEEP_PID=$!
    wait "$SLEEP_PID"
    SLEEP_PID=
done
