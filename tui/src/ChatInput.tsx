import React, { useState } from 'react';
import { Box, Text } from 'ink';
import TextInput from 'ink-text-input';

export interface ChatInputProps {
  onSubmit: (text: string) => void;
  isDisabled: boolean;
}

export function ChatInput({ onSubmit, isDisabled }: ChatInputProps) {
  const [value, setValue] = useState('');

  function handleSubmit(text: string) {
    const trimmed = text.trim();
    if (!trimmed) return;
    onSubmit(trimmed);
    setValue('');
  }

  if (isDisabled) {
    return (
      <Box>
        <Text dimColor>waiting for response...</Text>
      </Box>
    );
  }

  return (
    <Box>
      <Text bold color="cyan">{'> '}</Text>
      <TextInput
        value={value}
        onChange={setValue}
        onSubmit={handleSubmit}
        placeholder="Type a message and press Enter..."
      />
    </Box>
  );
}
