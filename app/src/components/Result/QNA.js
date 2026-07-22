import React from 'react';
import PropTypes from 'prop-types';
import { Table, Icon } from 'semantic-ui-react';

const QNA = ({ questionsAndAnswers }) => {
  return (
    <Table celled striped selectable size="large">
      <Table.Header>
        <Table.Row>
          <Table.HeaderCell>No.</Table.HeaderCell>
          <Table.HeaderCell>Questions</Table.HeaderCell>
          <Table.HeaderCell>Your Answers</Table.HeaderCell>
          <Table.HeaderCell>Correct Answers</Table.HeaderCell>
          <Table.HeaderCell>Points</Table.HeaderCell>
          <Table.HeaderCell>Learn More</Table.HeaderCell>
        </Table.Row>
      </Table.Header>
      <Table.Body>
        {questionsAndAnswers.map((item, i) => (
          <Table.Row key={i + 1} positive={item.point === 1}>
            <Table.Cell>{i + 1}</Table.Cell>
            <Table.Cell>{item.question}</Table.Cell>
            <Table.Cell negative={item.point === 0}>
              {item.user_answer}
            </Table.Cell>
            <Table.Cell>{item.correct_answer}</Table.Cell>
            <Table.Cell>{item.point}</Table.Cell>
            <Table.Cell>
              {item.source && (
                <a
                  href={item.source}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  <Icon name="external" />
                  Wiki
                </a>
              )}
            </Table.Cell>
          </Table.Row>
        ))}
      </Table.Body>
    </Table>
  );
};

QNA.propTypes = {
  questionsAndAnswers: PropTypes.array.isRequired,
};

export default QNA;
