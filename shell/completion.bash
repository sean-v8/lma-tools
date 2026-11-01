_lma_complete() {
    local current="${COMP_WORDS[COMP_CWORD]}"
    local previous="${COMP_WORDS[COMP_CWORD-1]}"
    COMPREPLY=()
    case "$previous" in
        --config|--collections|--notes)
            compopt -o filenames
            mapfile -t COMPREPLY < <(compgen -f -- "$current")
            return ;;
        --collection|--identifier|--retries|--retry-sleep|--workers)
            return ;;
    esac
    local -a config_args=()
    local i
    for ((i=1; i<COMP_CWORD; i++)); do
        if [[ "${COMP_WORDS[i]}" == --config &&
            $((i+1)) -lt COMP_CWORD ]]; then
            config_args=(--config "${COMP_WORDS[i+1]}")
        fi
    done
    mapfile -t COMPREPLY < <(
        command lmau "${config_args[@]}" --complete "$current" 2>/dev/null
    )
}
complete -F _lma_complete lmau

# Register for an existing tmux attachment shortcut using complete -F.
_lma_tmux_sessions() {
    local current="${COMP_WORDS[COMP_CWORD]}" session
    COMPREPLY=()
    while IFS= read -r session; do
        if [[ "$session" == "$current"* ]]; then
            COMPREPLY+=("$session")
        fi
    done < <(tmux list-sessions -F '#{session_name}' 2>/dev/null)
}
