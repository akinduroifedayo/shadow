export const title = 'Shadow Trial: model choice to result and optional switch'

const labels = {
  Idle: 'No trial yet', Preparing: 'Preparing preview', HistoryBlocked: 'History unreadable, nothing started',
  SetupBlocked: 'Start blocked', Review: 'Review estimate and recipients', Queued: 'Trial queued',
  Running: 'Replaying and judging', Stopped: 'Trial stopped', Result: 'Saved result',
  Warning: 'Confirm warned switch', Gradual: 'Switching gradually', Watch: 'Watching new default',
  Paused: 'Switch paused', Switched: 'Switch complete', Undone: 'Switch undone', Manual: 'Manual model choice'
}

const actions = {
  ChooseModel: 'choose a model', HistoryUnreadable: 'recent history cannot be read', SetupBlocks: 'setup or budget blocks start',
  ShowConsent: 'preview is ready', Approve: 'confirm Try or Compare', ControllerStarts: 'controller starts',
  StopTrial: 'cancel or interruption', SaveResult: 'save the result', RequestWarning: 'request a warned switch',
  DeclineWarning: 'cancel warning', StartSwitch: 'confirm switch', Trip: 'a safeguard trips',
  ResumeSwitch: 'resume the switch', FinishSwitch: 'switch completes', Undo: 'undo the switch',
  ChangeModelManually: 'change the model manually'
}

export const state = v => labels[v.phase]
export const step = action => actions[action] ?? null
