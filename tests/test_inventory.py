import pytest
from django.urls import reverse

@pytest.mark.django_db
def test_get_inventory_success(client):
    response = client.get(reverse('get_inventory'))
    assert response.status_code == 200
    assert 'mango1' in response.json()
    assert 'mango2' in response.json()
