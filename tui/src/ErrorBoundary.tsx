import React from 'react';
import { Text } from 'ink';

interface State {
  error: Error | null;
}

export class ErrorBoundary extends React.Component<{ children: React.ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  render() {
    if (this.state.error) {
      return <Text color="red">TUI Error: {this.state.error.message}</Text>;
    }
    return this.props.children;
  }
}
