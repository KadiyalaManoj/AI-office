import pytest
from django.urls import reverse
from django.contrib.auth.models import User
import jwt

@pytest.mark.django_db
def test_register_success(client):
    response = client.post(reverse('register'), data={'username': 'testuser', 'password': 'testpassword'})
    assert response.status_code == 201
    assert User.objects.filter(username='testuser').exists()

@pytest.mark.django_db
def test_register_duplicate_user(client):
    User.objects.create(username='testuser', password='testpassword')
    response = client.post(reverse('register'), data={'username': 'testuser', 'password': 'testpassword'})
    assert response.status_code == 400
    assert 'Username already exists' in response.json()['error']

@pytest.mark.django_db
def test_login_success(client):
    User.objects.create(username='testuser', password='testpassword')
    response = client.post(reverse('login'), data={'username': 'testuser', 'password': 'testpassword'})
    assert response.status_code == 200
    assert 'token' in response.json()

@pytest.mark.django_db
def test_login_invalid_credentials(client):
    User.objects.create(username='testuser', password='testpassword')
    response = client.post(reverse('login'), data={'username': 'testuser', 'password': 'wrongpassword'})
    assert response.status_code == 401
    assert 'Invalid credentials' in response.json()
