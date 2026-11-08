import pytest
from django.urls import reverse
import jwt

@pytest.mark.django_db
def test_place_order_success(client):
    token = jwt.encode({'username': 'testuser'}, 'secret_key', algorithm='HS256')
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    response = client.post(reverse('place_order'))
    assert response.status_code == 200
    assert 'order_id' in response.json()

@pytest.mark.django_db
def test_place_order_invalid_token(client):
    client.credentials(HTTP_AUTHORIZATION='Bearer invalid_token')
    response = client.post(reverse('place_order'))
    assert response.status_code == 401
    assert 'Invalid token' in response.json()

@pytest.mark.django_db
def test_track_order_success(client):
    order_id = 'ORD12345'
    response = client.get(reverse('track_order', args=[order_id]))
    assert response.status_code == 200
    assert 'order_id' in response.json()
    assert response.json()['status'] == 'Shipped'
