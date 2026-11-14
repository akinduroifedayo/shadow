COMMAND_NAME = 'shadow-trial'


def command_prefix(terminal):
    return f'hermes {COMMAND_NAME}' if terminal else f'/{COMMAND_NAME}'
