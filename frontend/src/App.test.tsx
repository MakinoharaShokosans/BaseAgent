import { render, screen } from '@testing-library/react';
import App from './App';

describe('App skeleton', () => {
  it('renders the product name', () => {
    render(<App />);
    expect(screen.getByRole('heading', { name: 'Aegis' })).toBeInTheDocument();
  });
});
