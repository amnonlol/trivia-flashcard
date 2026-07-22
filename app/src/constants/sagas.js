// Manga-canon sagas in story order. `order` matches the `sagaOrder` the pipeline
// stamps on each question (see pipeline/generate_questions.py SAGA_BOUNDS) and
// powers the "up to saga" spoiler-scope filter. `value` 0 means "whole series"
// (no limit) and is the default.
const SAGAS = [
  { key: '0', text: 'Whole series', value: 0 },
  { key: '1', text: 'East Blue', value: 1 },
  { key: '2', text: 'Alabasta', value: 2 },
  { key: '3', text: 'Sky Island', value: 3 },
  { key: '4', text: 'Water 7', value: 4 },
  { key: '5', text: 'Thriller Bark', value: 5 },
  { key: '6', text: 'Summit War', value: 6 },
  { key: '7', text: 'Fish-Man Island', value: 7 },
  { key: '8', text: 'Dressrosa', value: 8 },
  { key: '9', text: 'Whole Cake Island', value: 9 },
  { key: '10', text: 'Wano Country', value: 10 },
  { key: '11', text: 'Final Saga', value: 11 },
];

export default SAGAS;
