import pytest
from django.urls import reverse
import jwt

@pytest.mark.django_db
def test_process_payment_success(client):
    token = jwt.encode({'username': 'testuser'}, 'secret_key', algorithm='HS256')
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    response = client.post(reverse('process_payment'))
    assert response.status_code == 200
    assert 'message' in response.json()

@pytest.mark.django_db
def test_process_payment_invalid_token(client):
    client.credentials(HTTP_AUTHORIZATION='Bearer invalid_token')
    response = client.post(reverse('process_payment'))
    assert response.status_code == 401
    assert 'Invalid token' in response.json()
