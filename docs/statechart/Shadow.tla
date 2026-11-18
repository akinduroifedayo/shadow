---- MODULE Shadow ----
EXTENDS TLC

VARIABLES phase, intent, consent, verdict, mode, override
vars == <<phase, intent, consent, verdict, mode, override>>

Init ==
    /\ phase = "Idle"
    /\ intent = "none"
    /\ consent = FALSE
    /\ verdict = "none"
    /\ mode = "none"
    /\ override = FALSE

ChooseModel ==
    /\ phase \in {"Idle", "HistoryBlocked", "SetupBlocked", "Review"}
    /\ phase' = "Preparing"
    /\ UNCHANGED <<intent, consent, verdict, mode, override>>

\* A failed, slow, or malformed history read ends in an explicit reason, not a stalled picker.
HistoryUnreadable ==
    /\ phase = "Preparing"
    /\ phase' = "HistoryBlocked"
    /\ UNCHANGED <<intent, consent, verdict, mode, override>>

SetupBlocks ==
    /\ phase = "Preparing"
    /\ phase' = "SetupBlocked"
    /\ UNCHANGED <<intent, consent, verdict, mode, override>>

ShowConsent ==
    /\ phase = "Preparing"
    /\ phase' = "Review"
    /\ UNCHANGED <<intent, consent, verdict, mode, override>>

EstimateChanges ==
    /\ phase = "Review"
    /\ UNCHANGED vars

Approve ==
    /\ phase = "Review"
    /\ phase' = "Queued"
    /\ intent' \in {"try", "compare"}
    /\ consent' = TRUE
    /\ UNCHANGED <<verdict, mode, override>>

ControllerStarts ==
    /\ phase = "Queued"
    /\ phase' = "Running"
    /\ UNCHANGED <<intent, consent, verdict, mode, override>>

StopTrial ==
    /\ phase \in {"Queued", "Running"}
    /\ phase' = "Stopped"
    /\ UNCHANGED <<intent, consent, verdict, mode, override>>

SaveResult ==
    /\ phase = "Running"
    /\ phase' = "Result"
    /\ verdict' \in {"supports", "warn", "blocked"}
    /\ UNCHANGED <<intent, consent, mode, override>>

RequestWarning ==
    /\ phase = "Result" /\ intent = "try" /\ verdict = "warn"
    /\ phase' = "Warning"
    /\ UNCHANGED <<intent, consent, verdict, mode, override>>

DeclineWarning ==
    /\ phase = "Warning"
    /\ phase' = "Result"
    /\ UNCHANGED <<intent, consent, verdict, mode, override>>

StartSwitch ==
    /\ intent = "try"
    /\ \/ phase = "Result" /\ verdict = "supports"
       \/ phase = "Warning" /\ verdict = "warn"
    /\ mode' \in {"gradual", "all_at_once"}
    /\ phase' = IF mode' = "gradual" THEN "Gradual" ELSE "Watch"
    /\ override' = (phase = "Warning")
    /\ UNCHANGED <<intent, consent, verdict>>

Trip ==
    /\ phase \in {"Gradual", "Watch"}
    /\ phase' = "Paused"
    /\ UNCHANGED <<intent, consent, verdict, mode, override>>

ResumeSwitch ==
    /\ phase = "Paused"
    /\ phase' = IF mode = "gradual" THEN "Gradual" ELSE "Watch"
    /\ UNCHANGED <<intent, consent, verdict, mode, override>>

FinishSwitch ==
    /\ phase \in {"Gradual", "Watch"}
    /\ phase' = "Switched"
    /\ UNCHANGED <<intent, consent, verdict, mode, override>>

Undo ==
    /\ phase \in {"Gradual", "Watch", "Paused", "Switched"}
    /\ phase' = "Undone"
    /\ UNCHANGED <<intent, consent, verdict, mode, override>>

ChangeModelManually ==
    /\ phase \in {"Gradual", "Watch", "Paused", "Switched"}
    /\ phase' = "Manual"
    /\ UNCHANGED <<intent, consent, verdict, mode, override>>

Done ==
    /\ phase \in {"Stopped", "Result", "Undone", "Manual"}
    /\ UNCHANGED vars

Next ==
    \/ ChooseModel \/ HistoryUnreadable \/ SetupBlocks \/ ShowConsent
    \/ EstimateChanges \/ Approve \/ ControllerStarts \/ StopTrial \/ SaveResult
    \/ RequestWarning \/ DeclineWarning \/ StartSwitch \/ Trip \/ ResumeSwitch
    \/ FinishSwitch \/ Undo \/ ChangeModelManually \/ Done

TrialRequiresConsent == phase \in {"Queued", "Running", "Stopped", "Result"} => consent
CompareCannotSwitch == mode # "none" => intent = "try"
WarnedSwitchRequiresOverride == (mode # "none" /\ verdict = "warn") => override
\* A preview never ends in an unexplained error: every outcome is the consent card or a named block.
PreviewReturnsDecision == phase # "PreviewError"
====
