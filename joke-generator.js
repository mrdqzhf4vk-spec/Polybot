const fetch = require('node-fetch');

const API_URL = 'https://v2.jokeapi.dev/joke/Any';

async function getRandomJoke() {
    try {
        const response = await fetch(API_URL);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        const jokeData = await response.json();
        return formatJoke(jokeData);
    } catch (error) {
        console.error('Error fetching joke:', error.message);
        return 'Could not fetch a joke at this time.';
    }
}

function formatJoke(jokeData) {
    if (jokeData.type === 'single') {
        return `Joke: ${jokeData.joke}`;
    } else {
        return `Setup: ${jokeData.setup}\nDelivery: ${jokeData.delivery}`;
    }
}

// Example usage:
getRandomJoke().then(joke => console.log(joke));