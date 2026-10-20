const tasks = [
  { id: '1', title: 'Ship the notes', priority: 'High', body: 'Send the meeting notes today.' },
  { id: '2', title: 'Plan the demo', priority: 'Low', body: 'Pick three screens for the tour.' },
];

export default {
  mocks: [{ url: '**/api/tasks', json: tasks }],
  groups: [
    {
      id: 'A',
      title: 'Inbox',
      slide: { title: 'Inbox', text: 'See what needs a reply.' },
      setup: async (page, base) => { await page.goto(base + '/'); },
      cues: [
        { id: 'A.1', target: '#col-priority' },
        { id: 'A.2', target: '#open-1', action: 'click', after: async (page) => page.waitForSelector('#reply') },
      ],
    },
    {
      id: 'B',
      title: 'Reply',
      slide: { title: 'Reply', text: 'Answer on the same screen.' },
      cues: [{ id: 'B.1', target: '#reply' }],
    },
  ],
};
