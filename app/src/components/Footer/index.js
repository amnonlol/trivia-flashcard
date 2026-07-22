import React from 'react';
import { Container } from 'semantic-ui-react';

const Footer = () => {
  return (
    <Container textAlign="center" style={{ padding: '1.5em 0 2.5em' }}>
      <p style={{ color: '#888', fontSize: '0.85em' }}>
        A fan-made trivia trainer, not affiliated with One Piece or its
        creators. Question content is derived from the{' '}
        <a
          href="https://onepiece.fandom.com/"
          target="_blank"
          rel="noopener noreferrer"
        >
          One Piece Wiki
        </a>{' '}
        and is available under{' '}
        <a
          href="https://creativecommons.org/licenses/by-sa/3.0/"
          target="_blank"
          rel="noopener noreferrer"
        >
          CC BY-SA 3.0
        </a>
        .
      </p>
    </Container>
  );
};

export default Footer;
