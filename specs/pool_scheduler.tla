--------------------------- MODULE pool_scheduler ---------------------------
\* TLA+ specification of the Trellis pool scheduler.
\* Models: priority queue, cadence-aware scheduling, parallel groups,
\*         gating modes (auto/human/llm-decides), feedback scheduling,
\*         refinement loops, iteration caps, post-ready work,
\*         DEADLINE-as-success semantics, and per-idea background agents.
\*
\* Updated 2026-03-22 — revised semantics:
\*   - Feedback runs do NOT increment iterCount (feedback is human-directed)
\*   - Released ideas are NOT terminal — watchers + feedback run forever
\*   - Only killed ideas are truly terminal
\*   - DismissReview resets only the agent(s) that hit the iteration cap
\*
\* Implementation notes (Python divergences — intentional simplifications):
\*   - The Python uses continuous floating-point priorities rather than the
\*     discrete integers modeled here. The spec abstracts this to pipeline=5,
\*     feedback=4, background=3 for tractable model checking.
\*   - max_refinement_cycles=0 means infinite in Python (not modeled here;
\*     the spec uses a finite MaxRefinementCycles constant).
\*   - Kill is modeled atomically (queue drain). Python handles this lazily
\*     — killed-idea jobs may dispatch before the next producer scan.

EXTENDS Integers, Sequences, FiniteSets

CONSTANTS
    Workers,            \* Set of worker IDs
    Ideas               \* Set of idea IDs

\* Model values — hardcoded for tractable checking
PipelineAgents == <<"ideation", "validation">>
PostReadyAgents == {"watcher"}      \* post_ready roles (run after pipeline done)
BackgroundAgents == {"bg-watcher"}  \* cadence-based agents
AllAgentNames == {PipelineAgents[i] : i \in 1..Len(PipelineAgents)}
                 \cup PostReadyAgents \cup BackgroundAgents

\* Parallel groups: agents in the same group serialize on the same idea
SerialGroup == {PipelineAgents[i] : i \in 1..Len(PipelineAgents)}

\* MaxConcurrent: configurable per agent in registry (default 1)
MaxConcurrent(agent) == 1

\* Gating modes: "auto" | "human" | "llm-decides"
GatingModes == {"auto", "human", "llm-decides"}

\* Refinement: how many times pipeline can loop before terminal release
MaxRefinementCycles == 1
MaxIteratePerStage == 3

VARIABLES
    queue,              \* Set of [role, idea, kind, priority] records
    wState,             \* Function: worker -> "idle" | "running"
    wJob,               \* Function: worker -> record or NullJob
    phase,              \* Function: idea -> phase string
    stageResults,       \* Function: idea -> function: agent -> "pending"|"proceed"|"iterate"
    iterCount,          \* Function: idea -> function: agent -> nat (per-stage iteration count)
    releaseCount,       \* Function: idea -> nat (completed pipeline passes)
    needsReview,        \* Function: idea -> BOOLEAN (human review gate)
    hasFeedback,        \* Function: idea -> function: agent -> BOOLEAN
    cadenceDue,         \* Function: background_agent -> BOOLEAN
    runCount,           \* Function: agent -> nat (currently running instances)
    postReadyDone,      \* Function: idea -> function: post_ready_agent -> BOOLEAN
    gatingMode          \* Function: idea -> gating mode string

vars == <<queue, wState, wJob, phase, stageResults, iterCount,
          releaseCount, needsReview, hasFeedback, cadenceDue, runCount,
          postReadyDone, gatingMode>>

\* Sentinel values
NullJob == [role |-> "NONE", idea |-> "NONE", kind |-> "NONE", priority |-> 0]

------------------------------------------------------------------------
\* Helpers

PipelineAgentSet == {PipelineAgents[i] : i \in 1..Len(PipelineAgents)}

\* Next pipeline agent: first in sequence with result "pending" or "iterate"
NextAgent(idea) ==
    LET pendingIdx == {i \in 1..Len(PipelineAgents) :
            stageResults[idea][PipelineAgents[i]] \in {"pending", "iterate"}}
    IN IF pendingIdx = {}
       THEN "NONE"
       ELSE PipelineAgents[CHOOSE i \in pendingIdx : \A j \in pendingIdx : i <= j]

PipelineDone(idea) ==
    \A i \in 1..Len(PipelineAgents) : stageResults[idea][PipelineAgents[i]] = "proceed"

AllPostReadyDone(idea) ==
    \A a \in PostReadyAgents : postReadyDone[idea][a]

\* Group conflict: another agent from the serial group is running on this idea
HasConflict(agent, idea) ==
    /\ agent \in SerialGroup
    /\ \E w \in Workers :
        /\ wState[w] = "running"
        /\ wJob[w] # NullJob
        /\ wJob[w].idea = idea
        /\ wJob[w].role \in SerialGroup
        /\ wJob[w].role # agent

IsRunning(agent, idea) ==
    \E w \in Workers :
        /\ wState[w] = "running"
        /\ wJob[w] # NullJob
        /\ wJob[w].role = agent
        /\ wJob[w].idea = idea

InQueue(agent, idea) ==
    \E j \in queue : j.role = agent /\ j.idea = idea

Schedulable(agent, idea) ==
    /\ runCount[agent] < MaxConcurrent(agent)
    /\ ~HasConflict(agent, idea)
    /\ ~IsRunning(agent, idea)

\* Phases where pipeline work is eligible
IsEligible(idea) ==
    /\ phase[idea] \notin {"killed", "paused", "released"}
    /\ ~needsReview[idea]

\* Released but still needs post-ready work
IsPostReadyEligible(idea) ==
    /\ PipelineDone(idea)
    /\ ~AllPostReadyDone(idea)
    /\ phase[idea] # "killed"

------------------------------------------------------------------------
\* Initial state
Init ==
    /\ queue = {}
    /\ wState = [w \in Workers |-> "idle"]
    /\ wJob = [w \in Workers |-> NullJob]
    /\ phase = [i \in Ideas |-> "submitted"]
    /\ stageResults = [i \in Ideas |->
                         [a \in AllAgentNames |-> "pending"]]
    /\ iterCount = [i \in Ideas |->
                      [a \in PipelineAgentSet |-> 0]]
    /\ releaseCount = [i \in Ideas |-> 0]
    /\ needsReview = [i \in Ideas |-> FALSE]
    /\ hasFeedback = [i \in Ideas |->
                        [a \in AllAgentNames |-> FALSE]]
    /\ cadenceDue = [a \in BackgroundAgents |-> FALSE]
    /\ runCount = [a \in AllAgentNames |-> 0]
    /\ postReadyDone = [i \in Ideas |->
                          [a \in PostReadyAgents |-> FALSE]]
    /\ gatingMode = [i \in Ideas |-> "auto"]

------------------------------------------------------------------------
\* ACTIONS

\* ── Producers ────────────────────────────────────────────────────

\* Producer: enqueue next pipeline agent for an eligible idea
ProducePipeline(idea) ==
    LET agent == NextAgent(idea)
    IN /\ IsEligible(idea)
       /\ ~PipelineDone(idea)
       /\ agent # "NONE"
       /\ ~InQueue(agent, idea)
       /\ ~IsRunning(agent, idea)
       /\ queue' = queue \cup {[role |-> agent, idea |-> idea,
                                 kind |-> "pipeline", priority |-> 5]}
       /\ UNCHANGED <<wState, wJob, phase, stageResults, iterCount,
                       releaseCount, needsReview, hasFeedback, cadenceDue,
                       runCount, postReadyDone, gatingMode>>

\* Producer: enqueue post-ready agent when pipeline is done but post-ready work remains
ProducePostReady(idea, agent) ==
    /\ agent \in PostReadyAgents
    /\ IsPostReadyEligible(idea)
    /\ ~postReadyDone[idea][agent]
    /\ ~InQueue(agent, idea)
    /\ ~IsRunning(agent, idea)
    /\ queue' = queue \cup {[role |-> agent, idea |-> idea,
                              kind |-> "pipeline", priority |-> 5]}
    /\ UNCHANGED <<wState, wJob, phase, stageResults, iterCount,
                    releaseCount, needsReview, hasFeedback, cadenceDue,
                    runCount, postReadyDone, gatingMode>>

\* Producer: enqueue feedback-driven work. Feedback runs on ALL non-killed
\* ideas — including released, review-gated, paused. This is by design:
\* watchers discover new info and submit feedback that pipeline agents
\* process into artifacts. The feedback loop runs forever on living ideas.
ProduceFeedback(idea, agent) ==
    /\ phase[idea] # "killed"
    /\ agent \in PipelineAgentSet
    /\ hasFeedback[idea][agent]
    /\ ~InQueue(agent, idea)
    /\ ~IsRunning(agent, idea)
    /\ queue' = queue \cup {[role |-> agent, idea |-> idea,
                              kind |-> "feedback", priority |-> 4]}
    /\ UNCHANGED <<wState, wJob, phase, stageResults, iterCount,
                    releaseCount, needsReview, hasFeedback, cadenceDue,
                    runCount, postReadyDone, gatingMode>>

\* Cadence fires for a background agent
CadenceTick(agent) ==
    /\ agent \in BackgroundAgents
    /\ ~cadenceDue[agent]
    /\ cadenceDue' = [cadenceDue EXCEPT ![agent] = TRUE]
    /\ UNCHANGED <<queue, wState, wJob, phase, stageResults, iterCount,
                    releaseCount, needsReview, hasFeedback, runCount,
                    postReadyDone, gatingMode>>

\* Producer: enqueue a due background agent PER IDEA (not global).
\* Runs on ALL non-killed ideas — watchers monitor forever by design.
ProduceBackground(agent, idea) ==
    /\ agent \in BackgroundAgents
    /\ cadenceDue[agent]
    /\ phase[idea] # "killed"
    /\ ~InQueue(agent, idea)
    /\ ~IsRunning(agent, idea)
    /\ queue' = queue \cup {[role |-> agent, idea |-> idea,
                              kind |-> "background", priority |-> 3]}
    /\ UNCHANGED <<wState, wJob, phase, stageResults, iterCount,
                    releaseCount, needsReview, hasFeedback, cadenceDue,
                    runCount, postReadyDone, gatingMode>>

\* External: human submits feedback targeting an agent on an idea
SubmitFeedback(idea, agent) ==
    /\ phase[idea] # "killed"
    /\ agent \in PipelineAgentSet
    /\ ~hasFeedback[idea][agent]
    /\ hasFeedback' = [hasFeedback EXCEPT ![idea][agent] = TRUE]
    /\ UNCHANGED <<queue, wState, wJob, phase, stageResults, iterCount,
                    releaseCount, needsReview, cadenceDue, runCount,
                    postReadyDone, gatingMode>>

\* External: human dismisses review gate.
\* Only resets iterCount for the agent(s) that hit the cap — minimal
\* intervention. Other agents keep their counters.
DismissReview(idea) ==
    /\ needsReview[idea]
    /\ needsReview' = [needsReview EXCEPT ![idea] = FALSE]
    /\ iterCount' = [iterCount EXCEPT ![idea] =
                      [a \in PipelineAgentSet |->
                        IF iterCount[idea][a] >= MaxIteratePerStage
                        THEN 0
                        ELSE iterCount[idea][a]]]
    /\ UNCHANGED <<queue, wState, wJob, phase, stageResults,
                    releaseCount, hasFeedback, cadenceDue, runCount,
                    postReadyDone, gatingMode>>

\* External: human changes gating mode for an idea
ChangeGating(idea, mode) ==
    /\ mode \in GatingModes
    /\ gatingMode[idea] # mode
    /\ gatingMode' = [gatingMode EXCEPT ![idea] = mode]
    /\ UNCHANGED <<queue, wState, wJob, phase, stageResults, iterCount,
                    releaseCount, needsReview, hasFeedback, cadenceDue,
                    runCount, postReadyDone>>

\* ── Dispatch ─────────────────────────────────────────────────────

\* Dispatch: idle worker takes the HIGHEST PRIORITY schedulable job
Dispatch(worker, job) ==
    /\ wState[worker] = "idle"
    /\ job \in queue
    /\ Schedulable(job.role, job.idea)
    /\ \A other \in queue :
        Schedulable(other.role, other.idea) => other.priority <= job.priority
    /\ queue' = queue \ {job}
    /\ wState' = [wState EXCEPT ![worker] = "running"]
    /\ wJob' = [wJob EXCEPT ![worker] = job]
    /\ runCount' = [runCount EXCEPT ![job.role] = @ + 1]
    /\ UNCHANGED <<phase, stageResults, iterCount, releaseCount,
                    needsReview, hasFeedback, cadenceDue, postReadyDone,
                    gatingMode>>

\* ── Completion: pipeline agent proceeds ──────────────────────────

CompleteProceed(worker) ==
    LET idea == wJob[worker].idea
        agent == wJob[worker].role
        kind == wJob[worker].kind
        newResults == [stageResults EXCEPT ![idea][agent] = "proceed"]
        allDone == \A i \in 1..Len(PipelineAgents) :
                        newResults[idea][PipelineAgents[i]] = "proceed"
        shouldLoop == allDone /\ releaseCount[idea] < MaxRefinementCycles
        shouldRelease == allDone /\ releaseCount[idea] >= MaxRefinementCycles
        mode == gatingMode[idea]
    IN /\ wState[worker] = "running"
       /\ wJob[worker] # NullJob
       /\ kind \in {"pipeline", "feedback"}
       /\ agent \in PipelineAgentSet
       \* Feedback runs do NOT touch iterCount — feedback is human-directed
       /\ CASE mode = "human" /\ kind = "pipeline" ->
               /\ needsReview' = [needsReview EXCEPT ![idea] = TRUE]
               /\ stageResults' = newResults
               /\ phase' = phase
               /\ releaseCount' = releaseCount
               /\ iterCount' = iterCount
               /\ postReadyDone' = postReadyDone
          [] mode = "llm-decides" /\ kind = "pipeline" ->
               \* Non-deterministic: agent may or may not flag for review
               \/ (/\ needsReview' = [needsReview EXCEPT ![idea] = TRUE]
                   /\ stageResults' = newResults
                   /\ phase' = phase
                   /\ releaseCount' = releaseCount
                   /\ iterCount' = iterCount
                   /\ postReadyDone' = postReadyDone)
               \/ (/\ stageResults' = IF shouldLoop
                                      THEN [newResults EXCEPT ![idea] =
                                            [a \in AllAgentNames |-> "pending"]]
                                      ELSE newResults
                   /\ phase' = IF shouldRelease
                                THEN [phase EXCEPT ![idea] = "released"]
                                ELSE phase
                   /\ releaseCount' = IF allDone
                                      THEN [releaseCount EXCEPT ![idea] = @ + 1]
                                      ELSE releaseCount
                   /\ iterCount' = [iterCount EXCEPT ![idea][agent] = 0]
                   /\ needsReview' = needsReview
                   /\ postReadyDone' = IF shouldLoop
                                       THEN [postReadyDone EXCEPT ![idea] =
                                             [a \in PostReadyAgents |-> FALSE]]
                                       ELSE postReadyDone)
          [] OTHER ->  \* "auto" for pipeline, OR any feedback proceed
               /\ stageResults' = IF shouldLoop
                                  THEN [newResults EXCEPT ![idea] =
                                        [a \in AllAgentNames |-> "pending"]]
                                  ELSE newResults
               /\ phase' = IF shouldRelease
                            THEN [phase EXCEPT ![idea] = "released"]
                            ELSE phase
               /\ releaseCount' = IF allDone
                                  THEN [releaseCount EXCEPT ![idea] = @ + 1]
                                  ELSE releaseCount
               \* Only pipeline runs reset iterCount; feedback doesn't touch it
               /\ iterCount' = IF kind = "pipeline"
                                THEN [iterCount EXCEPT ![idea][agent] = 0]
                                ELSE iterCount
               /\ needsReview' = needsReview
               /\ postReadyDone' = IF shouldLoop
                                   THEN [postReadyDone EXCEPT ![idea] =
                                         [a \in PostReadyAgents |-> FALSE]]
                                   ELSE postReadyDone
       /\ wState' = [wState EXCEPT ![worker] = "idle"]
       /\ runCount' = [runCount EXCEPT ![agent] = @ - 1]
       /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
       /\ UNCHANGED <<queue, hasFeedback, cadenceDue, gatingMode>>

\* ── Completion: pipeline agent iterates ──────────────────────────
\* Feedback runs do NOT increment iterCount or trigger the iteration cap.

CompleteIterate(worker) ==
    LET idea == wJob[worker].idea
        agent == wJob[worker].role
        kind == wJob[worker].kind
        isFeedback == kind = "feedback"
        iters == iterCount[idea][agent] + 1
        hitCap == iters >= MaxIteratePerStage
    IN /\ wState[worker] = "running"
       /\ wJob[worker] # NullJob
       /\ kind \in {"pipeline", "feedback"}
       /\ agent \in PipelineAgentSet
       /\ stageResults' = [stageResults EXCEPT ![idea][agent] = "iterate"]
       \* Feedback: don't touch iterCount or trigger review gate
       /\ iterCount' = IF isFeedback
                        THEN iterCount
                        ELSE [iterCount EXCEPT ![idea][agent] = iters]
       /\ needsReview' = IF ~isFeedback /\ hitCap
                          THEN [needsReview EXCEPT ![idea] = TRUE]
                          ELSE needsReview
       /\ wState' = [wState EXCEPT ![worker] = "idle"]
       /\ runCount' = [runCount EXCEPT ![agent] = @ - 1]
       /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
       /\ UNCHANGED <<queue, phase, releaseCount, hasFeedback, cadenceDue,
                       postReadyDone, gatingMode>>

\* ── Completion: pipeline agent hits DEADLINE (timeout) ───────────
\* Python treats DEADLINE as a successful completion — the agent's
\* phase_recommendation is read and applied the same as RunStatus.OK.

CompleteDeadline(worker) ==
    /\ wState[worker] = "running"
    /\ wJob[worker] # NullJob
    /\ wJob[worker].kind \in {"pipeline", "feedback"}
    /\ wJob[worker].role \in PipelineAgentSet
    /\ \/ CompleteProceed(worker)
       \/ CompleteIterate(worker)

\* ── Completion: post-ready agent finishes ─────────────────────────

CompletePostReady(worker) ==
    LET idea == wJob[worker].idea
        agent == wJob[worker].role
    IN /\ wState[worker] = "running"
       /\ wJob[worker] # NullJob
       /\ wJob[worker].kind = "pipeline"
       /\ agent \in PostReadyAgents
       /\ postReadyDone' = [postReadyDone EXCEPT ![idea][agent] = TRUE]
       /\ wState' = [wState EXCEPT ![worker] = "idle"]
       /\ runCount' = [runCount EXCEPT ![agent] = @ - 1]
       /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
       /\ UNCHANGED <<queue, phase, stageResults, iterCount, releaseCount,
                       needsReview, hasFeedback, cadenceDue, gatingMode>>

\* ── Completion: background agent finishes ────────────────────────

CompleteBack(worker) ==
    /\ wState[worker] = "running"
    /\ wJob[worker] # NullJob
    /\ wJob[worker].kind = "background"
    /\ wJob[worker].role \in BackgroundAgents
    /\ cadenceDue' = [cadenceDue EXCEPT ![wJob[worker].role] = FALSE]
    /\ wState' = [wState EXCEPT ![worker] = "idle"]
    /\ runCount' = [runCount EXCEPT ![wJob[worker].role] = @ - 1]
    /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
    /\ UNCHANGED <<queue, phase, stageResults, iterCount, releaseCount,
                    needsReview, hasFeedback, postReadyDone, gatingMode>>

\* ── Completion: feedback run clears the feedback flag ─────────────

CompleteFeedbackRun(worker) ==
    LET idea == wJob[worker].idea
        agent == wJob[worker].role
    IN /\ wState[worker] = "running"
       /\ wJob[worker] # NullJob
       /\ wJob[worker].kind = "feedback"
       /\ agent \in PipelineAgentSet
       /\ hasFeedback' = [hasFeedback EXCEPT ![idea][agent] = FALSE]
       /\ wState' = [wState EXCEPT ![worker] = "idle"]
       /\ runCount' = [runCount EXCEPT ![agent] = @ - 1]
       /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
       /\ UNCHANGED <<queue, phase, stageResults, iterCount, releaseCount,
                       needsReview, cadenceDue, postReadyDone, gatingMode>>

\* ── Completion: error (any agent) ────────────────────────────────
\* TLA+ verified finding: background agents MUST reset cadence on error
\* to prevent livelock (perpetually "due" → infinite retry loop)

CompleteError(worker) ==
    /\ wState[worker] = "running"
    /\ wJob[worker] # NullJob
    /\ wState' = [wState EXCEPT ![worker] = "idle"]
    /\ runCount' = [runCount EXCEPT ![wJob[worker].role] = @ - 1]
    /\ cadenceDue' = IF wJob[worker].role \in BackgroundAgents
                     THEN [cadenceDue EXCEPT ![wJob[worker].role] = FALSE]
                     ELSE cadenceDue
    /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
    /\ UNCHANGED <<queue, phase, stageResults, iterCount, releaseCount,
                    needsReview, hasFeedback, postReadyDone, gatingMode>>

\* ── Agent kills idea ─────────────────────────────────────────────

Kill(worker) ==
    LET idea == wJob[worker].idea
    IN /\ wState[worker] = "running"
       /\ wJob[worker] # NullJob
       /\ wJob[worker].role \in PipelineAgentSet
       /\ phase' = [phase EXCEPT ![idea] = "killed"]
       /\ queue' = {j \in queue : j.idea # idea}
       /\ wState' = [wState EXCEPT ![worker] = "idle"]
       /\ runCount' = [runCount EXCEPT ![wJob[worker].role] = @ - 1]
       /\ wJob' = [wJob EXCEPT ![worker] = NullJob]
       /\ UNCHANGED <<stageResults, iterCount, releaseCount,
                       needsReview, hasFeedback, cadenceDue,
                       postReadyDone, gatingMode>>

------------------------------------------------------------------------
Next ==
    \/ \E i \in Ideas : ProducePipeline(i)
    \/ \E i \in Ideas, a \in PostReadyAgents : ProducePostReady(i, a)
    \/ \E i \in Ideas, a \in PipelineAgentSet : ProduceFeedback(i, a)
    \/ \E a \in BackgroundAgents : CadenceTick(a)
    \/ \E a \in BackgroundAgents, i \in Ideas : ProduceBackground(a, i)
    \/ \E i \in Ideas, a \in PipelineAgentSet : SubmitFeedback(i, a)
    \/ \E i \in Ideas : DismissReview(i)
    \/ \E i \in Ideas, m \in GatingModes : ChangeGating(i, m)
    \/ \E w \in Workers, j \in queue : Dispatch(w, j)
    \/ \E w \in Workers : CompleteProceed(w)
    \/ \E w \in Workers : CompleteIterate(w)
    \/ \E w \in Workers : CompleteDeadline(w)
    \/ \E w \in Workers : CompletePostReady(w)
    \/ \E w \in Workers : CompleteBack(w)
    \/ \E w \in Workers : CompleteFeedbackRun(w)
    \/ \E w \in Workers : CompleteError(w)
    \/ \E w \in Workers : Kill(w)

\* Strong fairness
Fairness ==
    /\ \A i \in Ideas : SF_vars(ProducePipeline(i))
    /\ \A i \in Ideas, a \in PostReadyAgents : SF_vars(ProducePostReady(i, a))
    /\ \A i \in Ideas, a \in PipelineAgentSet : SF_vars(ProduceFeedback(i, a))
    /\ \A a \in BackgroundAgents : SF_vars(CadenceTick(a))
    /\ \A a \in BackgroundAgents, i \in Ideas : SF_vars(ProduceBackground(a, i))
    /\ \A i \in Ideas : SF_vars(DismissReview(i))
    /\ \A w \in Workers : SF_vars(\E j \in queue : Dispatch(w, j))
    /\ \A w \in Workers : SF_vars(CompleteProceed(w))
    /\ \A w \in Workers : SF_vars(CompleteIterate(w))
    /\ \A w \in Workers : SF_vars(CompleteDeadline(w))
    /\ \A w \in Workers : SF_vars(CompletePostReady(w))
    /\ \A w \in Workers : SF_vars(CompleteBack(w))
    /\ \A w \in Workers : SF_vars(CompleteFeedbackRun(w))
    /\ \A w \in Workers : SF_vars(CompleteError(w))
    /\ \A w \in Workers : SF_vars(Kill(w))

Spec == Init /\ [][Next]_vars /\ Fairness

------------------------------------------------------------------------
\* SAFETY INVARIANTS

\* No agent exceeds max_concurrent running instances
MaxConcurrentOK ==
    \A a \in AllAgentNames : runCount[a] <= MaxConcurrent(a)

\* Serial group agents never run simultaneously on the same idea
SerialGroupOK ==
    \A w1, w2 \in Workers :
        (w1 # w2
         /\ wState[w1] = "running" /\ wState[w2] = "running"
         /\ wJob[w1] # NullJob /\ wJob[w2] # NullJob
         /\ wJob[w1].idea = wJob[w2].idea)
        => ~(wJob[w1].role \in SerialGroup /\ wJob[w2].role \in SerialGroup)

\* runCount accurately reflects the number of running instances
RunCountOK ==
    \A a \in AllAgentNames :
        runCount[a] = Cardinality({w \in Workers :
            wState[w] = "running" /\ wJob[w] # NullJob /\ wJob[w].role = a})

\* Pipeline iteration count never exceeds cap (feedback doesn't increment)
IterCountBounded ==
    \A i \in Ideas, a \in PipelineAgentSet :
        iterCount[i][a] <= MaxIteratePerStage

\* Killed ideas get no new work in the queue
KilledIsTerminal ==
    \A j \in queue : phase[j.idea] # "killed"

\* No new pipeline or feedback work queued for killed ideas
NoNewWorkOnKilled ==
    \A j \in queue :
        j.kind \in {"pipeline", "feedback"} => phase[j.idea] # "killed"

------------------------------------------------------------------------
\* LIVENESS PROPERTIES

\* Every submitted idea eventually reaches released or killed
Progress == \A i \in Ideas :
    phase[i] = "submitted" ~> phase[i] \in {"released", "killed"}

\* Every due watcher eventually runs — unless all ideas are killed
WatcherProgress == \A a \in BackgroundAgents :
    (cadenceDue[a] = TRUE /\ \E i \in Ideas : phase[i] # "killed")
    ~> cadenceDue[a] = FALSE

\* Feedback is eventually consumed — unless the idea is killed
FeedbackProgress == \A i \in Ideas, a \in PipelineAgentSet :
    (hasFeedback[i][a] = TRUE /\ phase[i] # "killed")
    ~> (hasFeedback[i][a] = FALSE \/ phase[i] = "killed")

\* Review gates are eventually dismissed (by human action)
ReviewProgress == \A i \in Ideas :
    needsReview[i] = TRUE ~> needsReview[i] = FALSE

========================================================================
