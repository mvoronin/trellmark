export function createRequestLifetime() {
    let generation = 0;
    let inFlight = false;
    return {
        get inFlight() { return inFlight; },
        capture: () => generation,
        begin() {
            generation += 1;
            inFlight = true;
            return generation;
        },
        isCurrent: (ticket) => ticket === generation,
        finish(ticket) {
            if (ticket !== generation)
                return false;
            inFlight = false;
            return true;
        },
        invalidate() {
            generation += 1;
            inFlight = false;
        },
    };
}
